-- one-button POC optimiser library. Runs inside the pob-engine Lua state after a build is loaded
-- (pinnacle-locked config). Entry points: OB_main() reads JOB_PARAMS (JSON) and returns JSON.
--   mode "opt"     : 1-button build -> optimise triggered spells + meta/feeder supports
--   mode "convert" : manual caster -> meta-gem trigger setup + feeder, then optimise
local dkjson = require("dkjson")
local build = launch.main.modes["BUILD"]
local calcs = build.calcsTab.calcs
local P = dkjson.decode(JOB_PARAMS or "{}") or {}
local POWER = P.power or 20          -- PoB's Pinnacle "EnemyPower"
local function arr(t) return setmetatable(t, { __jsontype = "array" }) end
local function has(t, n) return t and SkillType[n] and t[SkillType[n]] and true or false end
local function num(v) if type(v) == "number" and v == v and v ~= math.huge and v ~= -math.huge then return v end return 0 end
local clock0 = os.clock()
local NEVALS = 0
local BUDGET = P.budget or 420  -- cpu seconds per job for the search

-- ------------------------------------------------------------------ meta-gem energy model
-- per-event energy (centienergy per monster power) from the gem data (constantStats)
local META = {
	MetaCastOnCritPlayer = { kind = "crit" },
	MetaCastOnElementalAilmentPlayer = { kind = "ailment" },
}
local function constStat(ge, name)
	for _, ss in ipairs(ge.statSets and { ge.statSets[1] } or {}) do
		for _, cs in ipairs(ss.constantStats or {}) do if cs[1] == name then return cs[2] end end
	end
	return 0
end
local function levelStat(ge, name, level)
	local ss = ge.statSets and ge.statSets[1]
	if not ss then return 0 end
	for i, s in ipairs(ss.stats or {}) do
		if s == name then
			local lv = ss.levels[math.min(level or 1, #ss.levels)] or ss.levels[#ss.levels]
			return lv and lv[i] or 0
		end
	end
	return constStat(ge, name)
end
local function qualStat(ge, name, q)
	for _, qs in ipairs(ge.qualityStats or {}) do if qs[1] == name then return qs[2] * (q or 0) end end
	return 0
end

local function isMetaGem(ge) return ge and not ge.support and has(ge.skillTypes, "Meta") and has(ge.skillTypes, "Triggers") end
local function groupMeta(g)
	for _, gem in ipairs(g.gemList) do
		local ge = gem.gemData and gem.gemData.grantedEffect
		if isMetaGem(ge) and gem.enabled ~= false then return gem, ge end
	end
end
local function primaryGem(as)
	local src = as.activeEffect.srcInstance
	return src and src.gemData and src.gemData.grantedEffect == as.activeEffect.grantedEffect and not src.gemData.grantedEffect.support
end
local function damaging(as)
	local st = as.skillTypes or {}
	return has(st, "Damage") or has(st, "Attack") or has(st, "DamageOverTime") or has(st, "DegenOnlySpellDamage")
end

-- energy gained per relevant event and max energy for a meta group
local function metaEnergy(g)
	local mg, mge = groupMeta(g)
	if not mg then return nil end
	local m = META[mge.id]
	local gen = levelStat(mge, "energy_generated_+%", mg.level) + qualStat(mge, "energy_generated_+%", mg.quality)
	local refund = 0
	for _, gem in ipairs(g.gemList) do
		local ge = gem.gemData and gem.gemData.grantedEffect
		if ge and ge.support and gem.enabled ~= false then
			gen = gen + constStat(ge, "energy_generated_+%")
			refund = refund + constStat(ge, "trigger_skills_refund_half_energy_spent_chance_%")
		end
	end
	local maxE = 0
	for _, gem in ipairs(g.gemList) do
		local ge = gem.gemData and gem.gemData.grantedEffect
		if ge and not ge.support and not isMetaGem(ge) and gem.enabled ~= false and has(ge.skillTypes, "Spell") then
			maxE = maxE + (ge.castTime or 0) * 100   -- 10 energy per 0.1 s base cast time
		end
	end
	local e = { meta = mge.name, metaId = mge.id, kind = m and m.kind or "unmodelled", gen = gen, refund = refund, maxEnergy = maxE }
	if mge.id == "MetaCastOnCritPlayer" then
		e.perCrit = constStat(mge, "cast_on_crit_gain_X_centienergy_per_monster_power_on_crit") / 100
	elseif mge.id == "MetaCastOnElementalAilmentPlayer" then
		e.perShock = constStat(mge, "cast_on_shock_gain_X_centienergy_per_monster_power_on_shock") / 100
		e.perIgnite = constStat(mge, "cast_on_ignite_gain_X_centienergy_per_monster_power_on_ignite") / 100
		e.perFreeze = constStat(mge, "cast_on_freeze_gain_X_centienergy_per_monster_power_on_freeze") / 100
	end
	return e
end

-- triggers/s the energy allows, given the feeder's event rates
local function energyTriggerRate(e, feeder)
	if not e or e.kind == "unmodelled" or e.maxEnergy <= 0 then return nil end
	local perSec = 0
	if e.kind == "crit" then
		perSec = feeder.hitsPerSec * feeder.crit * e.perCrit
	else
		-- freeze vs pinnacle needs buildup; PoB-PoE2 does not give a usable freeze-event rate -> ignored (conservative)
		perSec = feeder.hitsPerSec * (feeder.shock * e.perShock + feeder.ignite * e.perIgnite)
	end
	perSec = perSec * POWER * (1 + e.gen / 100) / (1 - 0.5 * math.min(e.refund, 100) / 100)
	return perSec / e.maxEnergy, perSec
end

-- ------------------------------------------------------------------ evaluation
local function evalSkill(gi, si)
	local g = build.skillsTab.socketGroupList[gi]
	build.mainSocketGroup = gi; g.mainActiveSkill = si
	if wipeGlobalCache then wipeGlobalCache() end
	NEVALS = NEVALS + 1
	local ok, env = pcall(calcs.buildOutput, build, "MAIN")
	if not ok then return nil, tostring(env) end
	return env.player.output, env
end

-- classify an active skill like corpus-eval/extract.lua
local function classify(as, g, inMeta)
	local st = as.skillTypes or {}
	local ge = as.activeEffect.grantedEffect
	if has(st, "Triggered") or has(st, "InbuiltTrigger") or (inMeta and not isMetaGem(ge)) then return "triggered" end
	if has(st, "Persistent") or has(st, "HasReservation") then return "persistent" end
	local f = as.skillFlags or {}
	if has(st, "Minion") or has(st, "CreatesMinion") or has(st, "SummonsTotem") or has(st, "Trapped") or has(st, "RemoteMined") or f.totem or f.trap or f.mine or f.minion then return "proxy" end
	return "manual"
end

-- constraint checker on the currently calculated build (displaySkillList must be fresh)
function OB_constraint()
	local manual = {}
	for gi, g in ipairs(build.skillsTab.socketGroupList) do
		if g.enabled ~= false and g.source ~= "Explode" then
			local inMeta = groupMeta(g) ~= nil
			local nsup = 0
			for _, gem in ipairs(g.gemList) do local ge = gem.gemData and gem.gemData.grantedEffect; if ge and ge.support and gem.enabled ~= false then nsup = nsup + 1 end end
			for si, as in ipairs(g.displaySkillList or {}) do
				local ge = as.activeEffect.grantedEffect
				local gd = as.activeEffect.gemData
				local weaponDefault = gd and gd.Tier == 0 and (((gd.variantId or ""):find("^PlayerDefault") ~= nil) or gd.weaponRequirements ~= nil)
				local itemGranted = g.source ~= nil
				local counts = not weaponDefault and (not itemGranted or nsup > 0 or build.mainSocketGroup == gi)
				local src = as.activeEffect.srcInstance
				if counts and (not src or src.enabled ~= false) and damaging(as) and classify(as, g, inMeta) == "manual" then
					manual[#manual + 1] = { gi = gi, si = si, name = ge.name, id = ge.id }
				end
			end
		end
	end
	return #manual <= 1, manual
end

local function defence(o)
	local d = { Life = num(o.Life), ES = num(o.EnergyShield), TotalEHP = num(o.TotalEHP), SpiritUnreserved = o.SpiritUnreserved or 0 }
	for _, el in ipairs({ "Fire", "Cold", "Lightning", "Chaos" }) do
		d[el] = num(o[el .. "Resist"])
		d[el .. "Capped"] = num(o[el .. "Resist"]) >= num(o[el .. "ResistMax"] or 75) - 0.5 and num(o[el .. "Resist"]) >= 75
	end
	return d
end

local FEEDER -- {gi, si}
-- full evaluation: feeder + every damaging skill in every enabled meta group
local function evaluate(fast)
	local res = { triggered = arr({}) }
	local o = evalSkill(FEEDER.gi, FEEDER.si)
	if not o then return nil end
	local avgHit = num(o.AverageHit)
	local feeder = {
		name = build.skillsTab.socketGroupList[FEEDER.gi].displaySkillList[FEEDER.si].activeEffect.grantedEffect.name,
		dps = num(o.CombinedDPS), speed = num(o.Speed),
		hitsPerSec = avgHit > 0 and num(o.TotalDPS) / avgHit or num(o.Speed) * num(o.HitChance) / 100,
		crit = math.min(num(o.CritChance), 100) / 100,
		shock = math.min(num(o.ShockChance), 100) / 100, ignite = math.min(num(o.IgniteChance), 100) / 100,
	}
	res.feeder = feeder
	res.def = defence(o)
	local raw, cor = feeder.dps, feeder.dps
	for gi, g in ipairs(build.skillsTab.socketGroupList) do
		if g.enabled ~= false and groupMeta(g) and g.source == nil then
			local e = metaEnergy(g)
			local rate, eps = energyTriggerRate(e, feeder)
			for si, as in ipairs(g.displaySkillList or {}) do
				local ge = as.activeEffect.grantedEffect
				local src = as.activeEffect.srcInstance
				if not isMetaGem(ge) and damaging(as) and primaryGem(as) and src.enabled ~= false then
					local so = evalSkill(gi, si)
					local d = so and num(so.CombinedDPS) or 0
					local pobRate = so and num(so.Speed) or 0
					local f = 1
					if rate and pobRate > 0 then f = math.min(1, rate / pobRate) end
					raw = raw + d; cor = cor + d * f
					res.triggered[#res.triggered + 1] = { gi = gi, si = si, name = ge.name, meta = e.meta, kind = e.kind, dps = d, pobRate = pobRate,
						energyRate = rate, energyPerSec = eps, maxEnergy = e.maxEnergy, gen = e.gen, factor = f, corrected = d * f }
				end
			end
		end
	end
	res.raw, res.corrected = raw, cor
	return res
end

local BASE
local function floorsOk(r, ignoreSpirit)
	if not r then return false, "eval" end
	local b, d = BASE.def, r.def
	for _, el in ipairs({ "Fire", "Cold", "Lightning", "Chaos" }) do
		if b[el .. "Capped"] and not d[el .. "Capped"] then return false, el .. " uncapped" end
	end
	if d.TotalEHP < 0.9 * b.TotalEHP then return false, "EHP" end
	if not ignoreSpirit and d.SpiritUnreserved < math.min(0, b.SpiritUnreserved) then return false, "spirit" end
	local ok = OB_constraint()
	if not ok then return false, "not 1-button" end
	return true
end

-- ------------------------------------------------------------------ gem helpers
local function usedSupports()
	local u = {}
	if not P.uniqueSupports then return u end
	for _, g in ipairs(build.skillsTab.socketGroupList) do
		if g.enabled ~= false then
			for _, gem in ipairs(g.gemList) do
				local gd = gem.gemData
				if gd and gd.grantedEffect and gd.grantedEffect.support and gem.enabled ~= false then u[gd.grantedEffect.id] = true end
			end
		end
	end
	return u
end
local function setGem(g, idx, gemId, level, quality)
	local gem = g.gemList[idx]
	local old = { gemId = gem.gemId, skillId = gem.skillId, nameSpec = gem.nameSpec, level = gem.level, quality = gem.quality, gemData = gem.gemData, enabled = gem.enabled }
	gem.gemId = gemId; gem.skillId = nil; gem.gemData = nil; gem.nameSpec = data.gems[gemId].name
	if level then gem.level = level end
	if quality then gem.quality = quality end
	gem.enabled = true
	build.skillsTab:ProcessSocketGroup(g)
	return old
end
local function restoreGem(g, idx, old)
	local gem = g.gemList[idx]
	for k, v in pairs(old) do gem[k] = v end
	if not old.gemId then gem.gemId = nil end
	build.skillsTab:ProcessSocketGroup(g)
end
local function charLevel() return build.characterLevel or 100 end
local function gemTierOk(gd)
	local tier = gd.Tier and gd.Tier > 0 and gd.Tier or nil
	if not tier then return true end
	-- PoE2 tier ladder: tier t usable from roughly level (t-1)*... keep simple: all tiers allowed at 90+
	return charLevel() >= 65 or tier <= math.ceil(charLevel() / 6)
end
-- candidate supports that can support active skill `as`, not used elsewhere, families respected
local function supportCandidates(as, used, g)
	local fam = {}
	for _, gem in ipairs(g.gemList) do
		local gd = gem.gemData
		if gd and gd.grantedEffect and gd.grantedEffect.support then for _, f in ipairs(gd.grantedEffect.gemFamily or {}) do fam[f] = true end end
	end
	local out = {}
	for gemId, gd in pairs(data.gems) do
		local ge = gd.grantedEffect
		if ge and ge.support and not used[ge.id] and not ge.hidden and gemTierOk(gd) and not (gd.name or ""):find("'") and not gd.secondaryGrantedEffect and not gd.secondaryGrantedEffectId and not gd.additionalGrantedEffectId1 then
			local ok, s = pcall(calcLib.canGrantedEffectSupportActiveSkill, ge, as)
			if ok and s then out[#out + 1] = { gemId = gemId, name = gd.name, fam = ge.gemFamily } end
		end
	end
	return out
end
-- triggerable damaging spells (for spell swaps / conversions)
local function spellCandidates()
	local out = {}
	for gemId, gd in pairs(data.gems) do
		local ge = gd.grantedEffect
		local st = ge and ge.skillTypes or {}
		if ge and not ge.support and not ge.hidden and has(st, "Spell") and has(st, "Triggerable") and (has(st, "Damage") or has(st, "DamageOverTime"))
			and not has(st, "Meta") and not has(st, "Persistent") and not has(st, "HasReservation") and not has(st, "Minion") and not has(st, "CreatesMinion")
			and not has(st, "InbuiltTrigger") and not has(st, "Channel") and (ge.castTime or 0) > 0 and gd.Tier and gd.Tier > 0 and not gd.weaponRequirements then
			out[#out + 1] = { gemId = gemId, name = gd.name, castTime = ge.castTime }
		end
	end
	table.sort(out, function(a, b) return a.name < b.name end)
	return out
end

-- ------------------------------------------------------------------ search
local LOG = arr({})
local CUR -- current full evaluation
local function timeLeft() return BUDGET - (os.clock() - clock0) end

-- try candidate list on slot (g, idx): screen with `screen(cand)` (cheap score), full-eval top K
local function trySlot(g, idx, cands, screen, K, what)
	local gem = g.gemList[idx]
	local curName = gem.gemData and gem.gemData.name or gem.nameSpec
	local scored = {}
	for _, c in ipairs(cands) do
		if timeLeft() < 0 then break end
		local old = setGem(g, idx, c.gemId, c.level, c.quality)
		local s = screen()
		restoreGem(g, idx, old)
		if s then scored[#scored + 1] = { c = c, s = s } end
	end
	table.sort(scored, function(a, b) return a.s > b.s end)
	local best, bestR = nil, CUR
	for k = 1, math.min(K, #scored) do
		local c = scored[k].c
		if scored[k].s <= CUR.corrected * 1.001 then break end
		local old = setGem(g, idx, c.gemId, c.level, c.quality)
		local r = evaluate()
		local ok, why = floorsOk(r)
		if ok and r.corrected > bestR.corrected * 1.002 then best, bestR = c, r end
		restoreGem(g, idx, old)
		if not ok then LOG[#LOG + 1] = { what = what, from = curName, to = c.name, rejected = why } end
	end
	if best then
		setGem(g, idx, best.gemId, best.level, best.quality)
		CUR = evaluate()
		LOG[#LOG + 1] = { what = what, from = curName, to = best.name, raw = CUR.raw, corrected = CUR.corrected }
		return true
	end
	return false
end

-- cheap screen for a meta-group change: re-evaluate only the spells in that group, keep the rest from CUR
local function screenMetaGroup(gi)
	return function()
		local g = build.skillsTab.socketGroupList[gi]
		-- refresh displaySkillList via an eval of the feeder group would cost one more eval; meta group's own list
		-- is refreshed by evaluating any of its skills, so eval first skill then enumerate
		local o = evalSkill(gi, 1)
		if not o then return nil end
		local e = metaEnergy(g)
		local rate = energyTriggerRate(e, CUR.feeder)
		local tot = CUR.corrected
		for _, t in ipairs(CUR.triggered) do if t.gi == gi then tot = tot - t.corrected end end
		for si, as in ipairs(g.displaySkillList or {}) do
			local ge = as.activeEffect.grantedEffect
			local src = as.activeEffect.srcInstance
			if not isMetaGem(ge) and damaging(as) and primaryGem(as) and src.enabled ~= false then
				local so = evalSkill(gi, si)
				local d = so and num(so.CombinedDPS) or 0
				local pr = so and num(so.Speed) or 0
				local f = (rate and pr > 0) and math.min(1, rate / pr) or 1
				tot = tot + d * f
			end
		end
		return tot
	end
end
-- cheap screen for a feeder change: feeder output + cached triggered dps rescaled by new energy rates
local function screenFeeder()
	local o = evalSkill(FEEDER.gi, FEEDER.si)
	if not o then return nil end
	local avgHit = num(o.AverageHit)
	local fd = { hitsPerSec = avgHit > 0 and num(o.TotalDPS) / avgHit or num(o.Speed), crit = math.min(num(o.CritChance), 100) / 100,
		shock = math.min(num(o.ShockChance), 100) / 100, ignite = math.min(num(o.IgniteChance), 100) / 100 }
	local tot = num(o.CombinedDPS)
	for _, t in ipairs(CUR.triggered) do
		local g = build.skillsTab.socketGroupList[t.gi]
		local rate = energyTriggerRate(metaEnergy(g), fd)
		local f = (rate and t.pobRate > 0) and math.min(1, rate / t.pobRate) or 1
		tot = tot + t.dps * f
	end
	return tot
end

local function supportSlots(g)
	local s = {}
	for i, gem in ipairs(g.gemList) do
		local ge = gem.gemData and gem.gemData.grantedEffect
		if ge and ge.support and gem.enabled ~= false then s[#s + 1] = i end
	end
	return s
end

local function optimiseMetaGroup(gi, doSpells)
	local g = build.skillsTab.socketGroupList[gi]
	if doSpells and timeLeft() > 0 then
		local spells = spellCandidates()
		for i, gem in ipairs(g.gemList) do
			local ge = gem.gemData and gem.gemData.grantedEffect
			if ge and not ge.support and not isMetaGem(ge) and has(ge.skillTypes, "Spell") and gem.enabled ~= false then
				local cands = {}
				for _, c in ipairs(spells) do cands[#cands + 1] = { gemId = c.gemId, name = c.name, level = gem.level, quality = gem.quality } end
				trySlot(g, i, cands, screenMetaGroup(gi), 4, "spell@" .. gi)
			end
		end
	end
	for _, i in ipairs(supportSlots(g)) do
		if timeLeft() < 0 then break end
		evalSkill(gi, 1)
		local as
		for _, a in ipairs(g.displaySkillList or {}) do if not isMetaGem(a.activeEffect.grantedEffect) and damaging(a) then as = a break end end
		if as then
			local used = usedSupports()
			local cur = g.gemList[i].gemData
			if cur then used[cur.grantedEffect.id] = nil end
			-- families: exclude families present in the group other than this slot's own
			local cands = {}
			local famHere = {}
			for j, gem in ipairs(g.gemList) do
				if j ~= i and gem.gemData and gem.gemData.grantedEffect.support then for _, f in ipairs(gem.gemData.grantedEffect.gemFamily or {}) do famHere[f] = true end end
			end
			for _, c in ipairs(supportCandidates(as, used, { gemList = {} })) do
				local clash = false
				for _, f in ipairs(c.fam or {}) do if famHere[f] then clash = true end end
				if not clash and c.gemId ~= (cur and cur.id) then cands[#cands + 1] = { gemId = c.gemId, name = c.name, level = 1, quality = 0 } end
			end
			-- energy supports (support the meta gem, not the spell) are also candidates
			for gemId, gd in pairs(data.gems) do
				local ge = gd.grantedEffect
				if ge and ge.support and not used[ge.id] and (constStat(ge, "energy_generated_+%") > 0 or constStat(ge, "trigger_skills_refund_half_energy_spent_chance_%") > 0) then
					local clash = false
					for _, f in ipairs(ge.gemFamily or {}) do if famHere[f] then clash = true end end
					if not clash then cands[#cands + 1] = { gemId = gemId, name = gd.name, level = 1, quality = 0 } end
				end
			end
			trySlot(g, i, cands, screenMetaGroup(gi), 3, "metasupport@" .. gi)
		end
	end
end

local function optimiseFeeder()
	local g = build.skillsTab.socketGroupList[FEEDER.gi]
	for _, i in ipairs(supportSlots(g)) do
		if timeLeft() < 0 then break end
		evalSkill(FEEDER.gi, FEEDER.si)
		local as = g.displaySkillList[FEEDER.si]
		local used = usedSupports()
		local cur = g.gemList[i].gemData
		if cur then used[cur.grantedEffect.id] = nil end
		local famHere = {}
		for j, gem in ipairs(g.gemList) do
			if j ~= i and gem.gemData and gem.gemData.grantedEffect.support then for _, f in ipairs(gem.gemData.grantedEffect.gemFamily or {}) do famHere[f] = true end end
		end
		local cands = {}
		for _, c in ipairs(supportCandidates(as, used, { gemList = {} })) do
			local clash = false
			for _, f in ipairs(c.fam or {}) do if famHere[f] then clash = true end end
			if not clash and c.gemId ~= (cur and cur.id) then cands[#cands + 1] = { gemId = c.gemId, name = c.name, level = 1, quality = 0 } end
		end
		trySlot(g, i, cands, screenFeeder, 3, "feedersupport")
	end
end

local function exportCode()
	local xml = build:SaveDB("code")
	return common.base64.encode(Deflate(xml)):gsub("+", "-"):gsub("/", "_")
end

local function groupsSummary()
	local out = arr({})
	for gi, g in ipairs(build.skillsTab.socketGroupList) do
		if g.enabled ~= false and g.source == nil then
			local names = arr({})
			for _, gem in ipairs(g.gemList) do if gem.enabled ~= false then names[#names + 1] = (gem.gemData and gem.gemData.name) or gem.nameSpec end end
			out[#out + 1] = { gi = gi, gems = names, meta = groupMeta(g) and true or false }
		end
	end
	return out
end

local function findFeeder()
	local o = evalSkill(build.mainSocketGroup or 1, (build.skillsTab.socketGroupList[build.mainSocketGroup or 1] or {}).mainActiveSkill or 1)
	local ok, manual = OB_constraint()
	return ok, manual
end

-- ------------------------------------------------------------------ conversion (manual caster -> 1-button)
local function gemIdByName(name)
	for id, gd in pairs(data.gems) do if gd.name == name and gd.grantedEffect and not gd.grantedEffect.hidden then return id end end
end
local function newGem(gemId, level, quality)
	return { nameSpec = data.gems[gemId].name, gemId = gemId, level = level or 20, quality = quality or 0, enabled = true, count = 1, enableGlobal1 = true, enableGlobal2 = true }
end

function OB_main()
	local mode = P.mode or "opt"
	local out = { mode = mode }
	local ok, manual = findFeeder()
	out.before = { oneButton = ok, manual = manual }
	if mode == "convert" then
		-- the build's main group holds the spell to convert
		local mgi = build.mainSocketGroup
		local mg = build.skillsTab.socketGroupList[mgi]
		local mainGem
		for _, gem in ipairs(mg.gemList) do local ge = gem.gemData and gem.gemData.grantedEffect; if ge and not ge.support then mainGem = gem break end end
		out.converted = mainGem and mainGem.gemData.name
		-- baseline = manual main-skill DPS
		local ob = evalSkill(mgi, mg.mainActiveSkill or 1)
		out.baseline = { raw = num(ob.CombinedDPS), corrected = num(ob.CombinedDPS), def = defence(ob) }
		BASE = { def = out.baseline.def }
		local lvl, q = mainGem.level, mainGem.quality
		-- disable every other manual damaging group (it would break the constraint)
		for _, m in ipairs(manual) do
			if m.gi ~= mgi then build.skillsTab.socketGroupList[m.gi].enabled = false end
		end
		local best
		local function applyCombo(metaName, feederName, supIds)
			local metaId, fid = gemIdByName(metaName), gemIdByName(feederName)
			table.insert(mg.gemList, 1, newGem(metaId, math.max(lvl, 20), 0))
			build.skillsTab:ProcessSocketGroup(mg)
			local fg = { label = "1B feeder", enabled = true, gemList = { newGem(fid, lvl, q) } }
			for _, sid in ipairs(supIds or {}) do fg.gemList[#fg.gemList + 1] = newGem(sid, 1, 0) end
			table.insert(build.skillsTab.socketGroupList, fg)
			build.skillsTab:ProcessSocketGroup(fg)
			FEEDER = { gi = #build.skillsTab.socketGroupList, si = 1 }
			return fg, FEEDER.gi, fid
		end
		local function undoCombo(fgi)
			table.remove(build.skillsTab.socketGroupList, fgi)
			table.remove(mg.gemList, 1)
			build.skillsTab:ProcessSocketGroup(mg)
		end
		for _, metaName in ipairs(P.metas or { "Cast on Critical", "Cast on Elemental Ailment" }) do
			for _, feederName in ipairs(P.feeders or { "Spark" }) do
				if timeLeft() < BUDGET * 0.4 then break end
				local fg, fgi, fid = applyCombo(metaName, feederName, {})
				-- spirit: the meta gem reserves spirit; if that breaks the spirit floor, disable the persistent
				-- group whose loss costs least while keeping every floor
				CUR = evaluate()
				local disabled = nil
				local tries = 0
				while CUR and CUR.def.SpiritUnreserved < math.min(0, BASE.def.SpiritUnreserved) and tries < (P.maxDisable or 3) do
					tries = tries + 1
					local bestD
					for gi, g in ipairs(build.skillsTab.socketGroupList) do
						local as = g.displaySkillList and g.displaySkillList[1]
						if g.enabled ~= false and g.source == nil and gi ~= mgi and gi ~= fgi and as and not groupMeta(g)
							and (has(as.skillTypes, "Persistent") or has(as.skillTypes, "HasReservation")) then
							g.enabled = false
							local r = evaluate()
							local okd = floorsOk(r, true)
							if okd and (not bestD or r.def.SpiritUnreserved - BASE.def.SpiritUnreserved >= 0 and bestD.sp < 0 or r.corrected > bestD.c) then
								bestD = { gi = gi, c = r.corrected, sp = r.def.SpiritUnreserved - BASE.def.SpiritUnreserved, name = as.activeEffect.grantedEffect.name }
							end
							g.enabled = true
						end
					end
					if not bestD then break end
					build.skillsTab.socketGroupList[bestD.gi].enabled = false
					disabled = disabled or { list = {} }
					disabled.list[#disabled.list + 1] = bestD
					CUR = evaluate()
				end
				if disabled then
					local names = {}
					for _, x in ipairs(disabled.list) do names[#names + 1] = x.name end
					disabled.name = table.concat(names, "+")
				end
				-- greedy-fill feeder supports
				for k = 1, (P.feederSupports or 4) do
					evalSkill(fgi, 1)
					local used = usedSupports()
					local fam = {}
					for _, gem in ipairs(fg.gemList) do if gem.gemData and gem.gemData.grantedEffect.support then for _, f in ipairs(gem.gemData.grantedEffect.gemFamily or {}) do fam[f] = true end end end
					local as = fg.displaySkillList[1]
					local cands = {}
					for _, c in ipairs(supportCandidates(as, used, { gemList = {} })) do
						local clash = false
						for _, f in ipairs(c.fam or {}) do if fam[f] then clash = true end end
						if not clash then cands[#cands + 1] = { gemId = c.gemId, name = c.name, level = 1, quality = 0 } end
					end
					local ph = newGem(fid, 1, 0); ph.enabled = false
					fg.gemList[#fg.gemList + 1] = ph
					build.skillsTab:ProcessSocketGroup(fg)
					local idx = #fg.gemList
					CUR = evaluate()
					if not trySlot(fg, idx, cands, screenFeeder, 2, "feederfill") then table.remove(fg.gemList, idx); build.skillsTab:ProcessSocketGroup(fg) end
				end
				CUR = evaluate()
				local okc, why = floorsOk(CUR)
				local sup = {}
				for j = 2, #fg.gemList do sup[#sup + 1] = fg.gemList[j].gemId end
				out.combos = out.combos or arr({})
				out.combos[#out.combos + 1] = { meta = metaName, feeder = feederName, raw = CUR.raw, corrected = CUR.corrected, floors = okc, why = why, disabledForSpirit = disabled and disabled.name }
				if okc and (not best or CUR.corrected > best.corrected) then
					best = { corrected = CUR.corrected, meta = metaName, feeder = feederName, sup = sup, disabled = disabled }
				end
				if disabled then for _, x in ipairs(disabled.list) do build.skillsTab.socketGroupList[x.gi].enabled = true end end
				undoCombo(fgi)
			end
		end
		if not best then out.err = "no conversion met floors"; out.log = LOG; return dkjson.encode(out) end
		applyCombo(best.meta, best.feeder, best.sup)
		if best.disabled then for _, x in ipairs(best.disabled.list) do build.skillsTab.socketGroupList[x.gi].enabled = false end end
		out.meta, out.feeder, out.disabledForSpirit = best.meta, best.feeder, best.disabled and best.disabled.name
		CUR = evaluate()
		out.afterConvert = { raw = CUR.raw, corrected = CUR.corrected }
	else
		if not ok or #manual ~= 1 then out.err = "no single manual feeder"; out.before.manual = manual; return dkjson.encode(out) end
		FEEDER = { gi = manual[1].gi, si = manual[1].si }
		CUR = evaluate()
		BASE = CUR
		out.baseline = { raw = CUR.raw, corrected = CUR.corrected, def = CUR.def, feeder = CUR.feeder, triggered = CUR.triggered }
	end
	-- optimisation passes: meta groups (spells then supports), then feeder supports
	local metaGroups = {}
	for gi, g in ipairs(build.skillsTab.socketGroupList) do
		local _, mge = groupMeta(g)
		if g.enabled ~= false and g.source == nil and mge and META[mge.id] then metaGroups[#metaGroups + 1] = gi end
	end
	for pass = 1, (P.passes or 1) do
		for _, gi in ipairs(metaGroups) do optimiseMetaGroup(gi, P.spells ~= false) end
		optimiseFeeder()
	end
	CUR = evaluate()
	local okf, why = floorsOk(CUR)
	local ob, man = OB_constraint()
	-- restore the feeder as main skill for the export
	build.mainSocketGroup = FEEDER.gi
	build.calcsTab:BuildOutput()
	out.final = { raw = CUR.raw, corrected = CUR.corrected, def = CUR.def, feeder = CUR.feeder, triggered = CUR.triggered, floors = okf, why = why, oneButton = ob, manual = man }
	out.groups = groupsSummary()
	out.log = LOG
	out.code = exportCode()
	out.evals = NEVALS
	out.cpu = os.clock() - clock0
	return dkjson.encode(out)
end
