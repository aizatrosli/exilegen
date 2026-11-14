-- Support optimiser for one socket group (runs inside the pob engine, build already loaded,
-- Pinnacle config locked, main group set). Params: {g, skill, K?}.
-- Returns JSON {base, mas, slots, before, best={ids,slots,dps,...}, strict={...}}.
local dkjson = require("dkjson")
local P = dkjson.decode([==[__PARAMS__]==])
local build = launch.main.modes["BUILD"]
local st = build.skillsTab
local calcs = build.calcsTab.calcs
local gi = tonumber(P.g)
local group = st.socketGroupList[gi]
if not group then return dkjson.encode({ err = "no group " .. tostring(gi) }) end
local function num(v) if type(v) == "number" and v == v and v ~= math.huge and v ~= -math.huge then return v end return 0 end

-- main active skill: the one named like the corpus main skill, else the group's current one
local mas = group.mainActiveSkill or 1
for i, as in ipairs(group.displaySkillList or {}) do
	local ge = as.activeEffect and as.activeEffect.grantedEffect
	if ge and P.skill and ge.name == P.skill then mas = i break end
end
group.mainActiveSkill = mas
build.mainSocketGroup = gi
local activeSkill = group.displaySkillList and group.displaySkillList[mas]
if not activeSkill then return dkjson.encode({ err = "no active skill in group" }) end
if group.source then return dkjson.encode({ err = "item-granted group (" .. tostring(group.source) .. ")" }) end
local viewMode = build.viewMode
build.viewMode = "CALCS"
if wipeGlobalCache then wipeGlobalCache() end
local calcFunc = calcs.getMiscCalculator(build)
local FAST = { nodeAlloc = true, requirementsItems = true, requirementsGems = true, skipEHP = true }
local nEval, nFull = 0, 0
local function dpsOf(o) return math.max(num(o.CombinedDPS), num((o.Minion or {}).CombinedDPS)) end
local function evalFast()
	nEval = nEval + 1
	local ok, o = pcall(calcFunc, nil, false, FAST)
	if not ok or not o then return -1 end
	return dpsOf(o)
end
local function evalFull()
	nFull = nFull + 1
	local ok, o = pcall(calcFunc, nil, false, {})
	if not ok or not o then return nil end
	local r = { dps = dpsOf(o), ehp = num(o.TotalEHP), spirit = num(o.SpiritUnreserved), viol = {} }
	for _, e in ipairs({ "Fire", "Cold", "Lightning" }) do r[e] = num(o[e .. "Resist"]) end
	for _, a in ipairs({ "Str", "Dex", "Int" }) do
		if num(o["Req" .. a]) > num(o[a]) then r.viol[a] = true end
	end
	r.cost = num(o.ManaCost) + num(o.LifeCost)
	return r
end

local function supportsMain(ge)
	local ok, s = pcall(calcLib.canGrantedEffectSupportActiveSkill, ge, activeSkill)
	return ok and s
end

-- slots: enabled supports that support the main skill. Others (disabled, or supporting another
-- active skill in the group e.g. a trigger meta gem) are protected and never touched.
local slots, orig, protectedIds = {}, {}, {}
for i, gem in ipairs(group.gemList) do
	local ge = gem.gemData and gem.gemData.grantedEffect
	if ge and ge.support then
		if gem.enabled ~= false and supportsMain(ge) then
			slots[#slots + 1] = i
		else
			protectedIds[gem.gemData.id] = true
		end
	elseif gem.gemData then
		protectedIds[gem.gemData.id] = true
	end
	orig[i] = gem
end
if #slots == 0 then build.viewMode = viewMode; return dkjson.encode({ err = "no optimisable supports" }) end

-- gems socketed anywhere else in the build (PoE2: each support once per character; lineage limit)
local usedElsewhere = {}
for j, g2 in ipairs(st.socketGroupList) do
	if j ~= gi and g2.enabled ~= false then
		for _, gem in ipairs(g2.gemList or {}) do
			if gem.gemData and gem.enabled ~= false then usedElsewhere[gem.gemData.id] = true end
		end
	end
end

-- conditional / best-case heuristic: description phrases implying a state PoB cannot know at a
-- Pinnacle fight (movement, kills, combo, charges, blocks, life thresholds, timers...)
local COND = { "recently", "kill", "moving", "movement", "full life", "low life", "every few seconds",
	"consume", "expend", "combo", "cannot be used until", "block", "stationary", "charges", "rage",
	"after ", "when hit", "while you", "if you", "overgrown", "corpse", "fully broken", "the more",
	"increasingly", "archon", "infusion", "remnant", "surge", "glory", "unbound", "on death", "die" }
local function condReasons(gd)
	local d = ((gd.grantedEffect and gd.grantedEffect.description) or ""):lower()
	local hits = {}
	for _, k in ipairs(COND) do if d:find("%f[%a]" .. k) then hits[#hits + 1] = k end end
	return hits
end

local charLevel = build.characterLevel or 100
local TIER_LEVEL = { 1, 3, 6, 10, 14, 18, 22, 26, 31, 36, 41, 46, 52, 58, 64, 66, 72, 78, 84, 90 }
local cands = {}
for id, gd in pairs(data.gems) do
	local ge = gd.grantedEffect
	if ge and ge.support and not ge.hidden and not usedElsewhere[id] and not protectedIds[id] and supportsMain(ge) then
		local req = gd.Tier and gd.Tier > 0 and TIER_LEVEL[gd.Tier] or nil
		if not (req and req > charLevel) then cands[#cands + 1] = id end
	end
end
table.sort(cands)

local function mkGem(id, proto)
	local gd = data.gems[id]
	local t = { level = 1, quality = 0, count = 1, enabled = true, enableGlobal1 = true, enableGlobal2 = true,
		gemId = gd.id, nameSpec = gd.name, skillId = gd.grantedEffectId }
	t.level = st:ProcessGemLevel(gd)
	t.gemData = gd
	return t
end
local function famOf(id)
	local ge = data.gems[id].grantedEffect
	local f = {}
	for _, x in ipairs(ge.gemFamily or {}) do f[x] = true end
	return f, ge
end
-- every support in the group other than the slots being replaced
local function otherSupportIds(assign, skip)
	local ids = {}
	for i, gem in ipairs(group.gemList) do
		local id
		local sIdx
		for k, s in ipairs(slots) do if s == i then sIdx = k end end
		if sIdx then id = (not skip[sIdx]) and assign[sIdx] or nil
		elseif gem.gemData and gem.gemData.grantedEffect.support and gem.enabled ~= false then id = gem.gemData.id end
		if id then ids[#ids + 1] = id end
	end
	return ids
end
local function conflicts(a, b)
	if a == b then return true end
	local fa, ga = famOf(a)
	local fb, gb = famOf(b)
	for k in pairs(fa) do if fb[k] then return true end end
	if ga.plusVersionOf == gb.id or gb.plusVersionOf == ga.id then return true end
	return false
end
-- assign: array (by slot k) of gem ids. replacement: {k=id,...}. legal?
local function legal(assign, repl)
	local skip = {}
	for k in pairs(repl) do skip[k] = true end
	local others = otherSupportIds(assign, skip)
	local newIds = {}
	for _, id in pairs(repl) do newIds[#newIds + 1] = id end
	for i, a in ipairs(newIds) do
		for j = i + 1, #newIds do if conflicts(a, newIds[j]) then return false end end
		for _, o in ipairs(others) do if conflicts(a, o) then return false end end
	end
	return true
end

local gemCache = {}
local function gemFor(id)
	if not gemCache[id] then gemCache[id] = mkGem(id) end
	return gemCache[id]
end
local function setAssign(assign)
	for k, s in ipairs(slots) do
		if assign[k] == orig[s].gemData.id then group.gemList[s] = orig[s] else group.gemList[s] = gemFor(assign[k]) end
	end
end
local function scoreWith(assign, repl, disable)
	for k, s in ipairs(slots) do
		local id = repl[k] or assign[k]
		group.gemList[s] = (id == orig[s].gemData.id) and orig[s] or gemFor(id)
	end
	local saved
	if disable then
		local s = slots[disable]
		saved = group.gemList[s]
		local c = {}
		for kk, v in pairs(saved) do c[kk] = v end
		c.enabled = false
		group.gemList[s] = c
	end
	local d = evalFast()
	if disable then group.gemList[slots[disable]] = saved end
	return d
end

local before = {}
for k, s in ipairs(slots) do before[k] = orig[s].gemData.id end
setAssign(before)
local base0 = evalFast()
local baseFull = evalFull()

local function guardOK(r)
	if not r then return false, "calc error" end
	if baseFull.ehp > 0 and r.ehp < baseFull.ehp * 0.999 then return false, "EHP" end
	for _, e in ipairs({ "Fire", "Cold", "Lightning" }) do if r[e] < baseFull[e] - 0.01 then return false, e .. " resist" end end
	for a in pairs(r.viol) do if not baseFull.viol[a] then return false, a .. " requirement" end end
	if r.spirit < 0 and r.spirit < baseFull.spirit then return false, "Spirit" end
	return true
end

local function optimise(allowed, label)
	local t0 = os.clock()
	local e0, f0 = nEval, nFull
	local assign = {}
	for k, v in ipairs(before) do assign[k] = v end
	local cur = scoreWith(assign, {})
	local log, rejected = {}, {}
	local EPS = 1.0005
	-- try improving moves (sorted best first), accepting the first one that passes the guard
	local function tryMoves(moves, phase)
		table.sort(moves, function(a, b) return a.dps > b.dps end)
		for _, m in ipairs(moves) do
			if m.dps <= cur * EPS then return false end
			local trial = {}
			for k, v in ipairs(assign) do trial[k] = m.repl[k] or v end
			setAssign(trial)
			local r = evalFull()
			local ok, why = guardOK(r)
			if ok then
				local desc = {}
				for k, id in pairs(m.repl) do desc[#desc + 1] = data.gems[assign[k]].name .. " -> " .. data.gems[id].name end
				log[#log + 1] = { phase = phase, swap = table.concat(desc, "; "), from = cur, to = r.dps }
				assign = trial
				cur = scoreWith(assign, {})
				return true
			else
				local desc = {}
				for k, id in pairs(m.repl) do desc[#desc + 1] = data.gems[id].name end
				rejected[#rejected + 1] = { add = table.concat(desc, "+"), why = why, dps = m.dps }
			end
		end
		return false
	end
	local inAssign = function(id) for _, v in ipairs(assign) do if v == id then return true end end return false end
	-- phase 1: greedy - weakest support (least removal loss) replaced by the best legal unused one
	local guard = 0
	while guard < 30 do
		guard = guard + 1
		local order = {}
		for k = 1, #slots do order[#order + 1] = { k = k, loss = cur - scoreWith(assign, {}, k) } end
		table.sort(order, function(a, b) return a.loss < b.loss end)
		local improved = false
		for _, o in ipairs(order) do
			local moves = {}
			for _, id in ipairs(cands) do
				if allowed(id) and not inAssign(id) and legal(assign, { [o.k] = id }) then
					moves[#moves + 1] = { repl = { [o.k] = id }, dps = scoreWith(assign, { [o.k] = id }) }
				end
			end
			if tryMoves(moves, "greedy") then improved = true break end
		end
		if not improved then break end
	end
	-- phase 2: local search - full single-swap neighbourhood + pairwise swaps among top-K candidates
	local K = tonumber(P.K) or 8
	guard = 0
	while guard < 20 do
		guard = guard + 1
		local moves, bestPer = {}, {}
		for k = 1, #slots do
			for _, id in ipairs(cands) do
				if allowed(id) and not inAssign(id) and legal(assign, { [k] = id }) then
					local d = scoreWith(assign, { [k] = id })
					moves[#moves + 1] = { repl = { [k] = id }, dps = d }
					bestPer[id] = math.max(bestPer[id] or 0, d)
				end
			end
		end
		local top = {}
		for id, d in pairs(bestPer) do top[#top + 1] = { id = id, d = d } end
		table.sort(top, function(a, b) return a.d > b.d end)
		for i = #top, K + 1, -1 do top[i] = nil end
		for k1 = 1, #slots do
			for k2 = k1 + 1, #slots do
				for i = 1, #top do
					for j = 1, #top do
						if i ~= j then
							local repl = { [k1] = top[i].id, [k2] = top[j].id }
							if legal(assign, repl) then moves[#moves + 1] = { repl = repl, dps = scoreWith(assign, repl) } end
						end
					end
				end
			end
		end
		if not tryMoves(moves, "local") then break end
	end
	setAssign(assign)
	local fin = evalFull()
	setAssign(before)
	local names, newCond = {}, {}
	for k, id in ipairs(assign) do
		names[k] = data.gems[id].name
		if id ~= before[k] then
			local isNew = true
			for _, b in ipairs(before) do if b == id then isNew = false end end
			local cr = condReasons(data.gems[id])
			if isNew and #cr > 0 then newCond[#newCond + 1] = { name = data.gems[id].name, why = table.concat(cr, ",") } end
		end
	end
	return { label = label, ids = assign, names = names, slots = slots, dps = fin and fin.dps or cur, fast_dps = cur,
		ehp = fin and fin.ehp, evals = nEval - e0, full_evals = nFull - f0, cpu_s = os.clock() - t0,
		log = setmetatable(log, { __jsontype = "array" }), rejected = setmetatable(rejected, { __jsontype = "array" }),
		new_conditional = setmetatable(newCond, { __jsontype = "array" }) }
end

local beforeSet = {}
for _, id in ipairs(before) do beforeSet[id] = true end
local best = optimise(function(id) return true end, "best")
local strict = optimise(function(id) return beforeSet[id] or #condReasons(data.gems[id]) == 0 end, "strict")

-- removal loss table for the original setup
local remLoss = {}
setAssign(before)
for k = 1, #slots do remLoss[#remLoss + 1] = { name = data.gems[before[k]].name, loss_pct = (base0 - scoreWith(before, {}, k)) / math.max(base0, 1) * 100 } end
setAssign(before)
build.viewMode = viewMode
local beforeNames, beforeCond = {}, {}
for k, id in ipairs(before) do
	beforeNames[k] = data.gems[id].name
	if #condReasons(data.gems[id]) > 0 then beforeCond[#beforeCond + 1] = data.gems[id].name end
end
return dkjson.encode({ mas = mas, skill = activeSkill.activeEffect.grantedEffect.name, base = base0, base_full = baseFull,
	slots = slots, before = beforeNames, before_conditional = setmetatable(beforeCond, { __jsontype = "array" }),
	n_cands = #cands, n_protected = (function() local n = 0 for _ in pairs(protectedIds) do n = n + 1 end return n end)(),
	removal_loss = remLoss, best = best, strict = strict, evals = nEval, full_evals = nFull })
