-- exg.lua: exilegen engine ops, installed once as the global EXG inside one pob-engine Lua state.
--
-- The Rust host (engine/src/main.rs) calls EXG.init(profilesJson) once after boot and then
-- EXG.call(requestJson) per request, where requestJson = {"op": name, "p": params}. Every op
-- returns a JSON string (dkjson). Ops are documented in engine/API.md.
--
-- Stats are always computed with calcWith() below: PoB's own calc pipeline (calcs.initEnv +
-- calcs.perform in "CALCULATOR" mode, the same path as PoB's calcFunc / node power), with an
-- override {addNodes, removeNodes, mainSocketGroup}. stats, whatif and apply therefore read the
-- same numbers for the same state.
local dkjson = require("dkjson")

EXG = EXG or {}
local E = EXG
E.VERSION = "1"

-- ---------------------------------------------------------------- helpers
local NULL = dkjson.null
E.NULL = NULL
local function B() return launch.main.modes["BUILD"] end
local function arr(t) return setmetatable(t or {}, { __jsontype = "array" }) end
local function obj(t) return setmetatable(t or {}, { __jsontype = "object" }) end
local function num(v) if type(v) == "number" and v == v and v ~= math.huge and v ~= -math.huge then return v end return nil end
local function n0(v) return num(v) or 0 end
local function isnull(v) return v == nil or v == NULL end

local STN = {}
for k, v in pairs(SkillType) do STN[v] = k end
local function typeNames(t)
	local r = arr({})
	if t then for k, v in pairs(t) do if v and STN[k] then r[#r + 1] = STN[k] end end end
	table.sort(r)
	return r
end
local function has(t, name) return t and SkillType[name] and t[SkillType[name]] and true or false end

local function copyArr(t)
	local r = arr({})
	for i, v in ipairs(t or {}) do r[i] = v end
	return r
end

local function keysSorted(t)
	local r = arr({})
	for k, v in pairs(t or {}) do if v then r[#r + 1] = k end end
	table.sort(r)
	return r
end

local function wantSet(fields, default)
	if isnull(fields) then fields = default end
	if type(fields) == "string" then fields = { fields } end
	local s = {}
	for _, f in ipairs(fields or {}) do
		if f == "core" then for _, g in ipairs(E.CORE_FIELDS) do s[g] = true end
		elseif f == "all" then for _, g in ipairs(E.ALL_FIELDS) do s[g] = true end
		else s[f] = true end
	end
	return s
end

-- ---------------------------------------------------------------- state
E.state = E.state or {}
local S = E.state

function E.before_load()
	S.snapshots, S.nextToken = {}, 1
	S.profile, S.profileSets, S.playerConfigSetId = nil, {}, nil
	S.main, S.mainSel, S.pobMain = nil, "auto", nil
	S.dirty = false
	if wipeGlobalCache then wipeGlobalCache() end
	return true
end

-- Garbage collection between loads. A full collection costs 0.3-0.5 s on a loaded build, so it
-- runs only when the heap has grown past E.GC_FULL_MB; otherwise one incremental step.
E.GC_FULL_MB = 600
function E.gc_between_loads(mode)
	if mode == "none" then return "none" end
	if mode == "full" or collectgarbage("count") / 1024 > E.GC_FULL_MB then
		collectgarbage("collect")
		return "full"
	end
	collectgarbage("step", 0)
	return "step"
end

function E.init(profilesJson)
	local p, _, err = dkjson.decode(profilesJson or "{}", 1, nil)
	if not p then error("profiles.json: " .. tostring(err), 0) end
	E.profiles = p.profiles or {}
	E.before_load()
	local names = arr({})
	for k in pairs(E.profiles) do names[#names + 1] = k end
	table.sort(names)
	return dkjson.encode({ version = E.VERSION, profiles = names })
end

-- PoB recalculation for its own state (sidebar, mainOutput). Only needed where PoB's own state
-- must follow (export, debugging); stats never read mainOutput.
local function refresh()
	local b = B()
	b.buildFlag = true
	b.modFlag = true
	local level = b.characterLevel
	runCallback("OnFrame")
	if b.characterLevel ~= level then
		b.buildFlag = true
		runCallback("OnFrame")
	end
	S.dirty = false
end
E.refresh = refresh

-- One calculation of the current state plus override. Returns env, output.
local function calcWith(override, skipEHP)
	local b = B()
	local calcs = b.calcsTab.calcs
	-- trigger calcs read GlobalCache by skill identity, not state: never reuse across variants
	if GlobalCache and GlobalCache.cachedData then wipeTable(GlobalCache.cachedData.CALCULATOR) end
	local env = calcs.initEnv(b, "CALCULATOR", override or {})
	env.override = override
	calcs.perform(env, skipEHP)
	return env, env.player.output
end
E.calcWith = calcWith

-- ---------------------------------------------------------------- gems
local GEM_TIER_LEVEL = { 1, 3, 6, 10, 14, 18, 22, 26, 31, 36, 41, 46, 52, 58, 64, 66, 72, 78, 84, 90 }
local ATTR_BY_COLOR = { [1] = "Str", [2] = "Dex", [3] = "Int" }

local gemIndex
local function findGem(key)
	if type(key) ~= "string" then return nil end
	if data.gems[key] then return data.gems[key] end
	if not gemIndex then
		gemIndex = { skill = {}, name = {} }
		for id, gd in pairs(data.gems) do
			if gd.grantedEffectId then gemIndex.skill[gd.grantedEffectId] = gemIndex.skill[gd.grantedEffectId] or gd end
			if gd.name then
				local prev = gemIndex.name[gd.name:lower()]
				-- prefer the gem that is not hidden
				if not prev or (prev.grantedEffect and prev.grantedEffect.hidden) then gemIndex.name[gd.name:lower()] = gd end
			end
		end
	end
	return gemIndex.skill[key] or gemIndex.name[key:lower()]
end
E.findGem = findGem

local function gemReqLevel(gd)
	local tier = gd and gd.Tier and gd.Tier > 0 and gd.Tier or nil
	return tier and GEM_TIER_LEVEL[tier] or nil, tier
end

local function isMetaGemEffect(ge) return ge and not ge.support and has(ge.skillTypes, "Meta") and has(ge.skillTypes, "Triggers") end

local function groupMeta(g)
	for i, gem in ipairs(g.gemList or {}) do
		local ge = gem.gemData and gem.gemData.grantedEffect
		if isMetaGemEffect(ge) and gem.enabled ~= false then return gem, ge, i end
	end
end

local function newGemInstance(gd, level, quality, enabled)
	return {
		nameSpec = gd.name, gemId = gd.id, skillId = gd.grantedEffectId,
		level = level, quality = quality or 0, enabled = enabled ~= false, count = 1,
		enableGlobal1 = true, enableGlobal2 = true, corrupted = false, corruptLevel = 0,
	}
end

local function copyGem(gem)
	local c = {}
	for k, v in pairs(gem) do c[k] = v end
	return c
end

-- Applies gem_changes / group_enabled to the live skills tab. Gem objects are never mutated in
-- place (a changed gem is a new table), so undo restores the saved arrays. Returns the undo record.
local function applyGemChanges(changes, groupEnabled)
	local st = B().skillsTab
	local list = st.socketGroupList
	local undo = { groupList = {}, groups = {}, touched = {} }
	for i, g in ipairs(list) do undo.groupList[i] = g end
	local function touch(g)
		if not undo.touched[g] then
			undo.touched[g] = true
			local saved = {}
			for i, gem in ipairs(g.gemList) do saved[i] = gem end
			undo.groups[#undo.groups + 1] = { g = g, gems = saved, enabled = g.enabled, mas = g.mainActiveSkill }
		end
	end
	local removals = {}
	for _, c in ipairs(changes or {}) do
		local gi = tonumber(c.group)
		if not gi then error("gem change needs group", 0) end
		local g = list[gi]
		if not g then
			if gi == #list + 1 then
				g = { label = c.label or "exg", enabled = true, includeInFullDPS = false, gemList = {}, mainActiveSkill = 1 }
				list[gi] = g
			else
				error("unknown group " .. tostring(c.group), 0)
			end
		end
		if g.source then error("group " .. gi .. " is granted by an item or node and cannot be changed", 0) end
		touch(g)
		local slot = tonumber(c.slot) or (#g.gemList + 1)
		local old = g.gemList[slot]
		if c.gem == NULL then
			if old then removals[#removals + 1] = { g = g, slot = slot } end
		else
			local gem
			if c.gem ~= nil then
				local gd = findGem(c.gem)
				if not gd then error("unknown gem " .. tostring(c.gem), 0) end
				local level = tonumber(c.level)
				if not level then
					local og = old and old.gemData and old.gemData.grantedEffect
					if og and not og.support and not gd.grantedEffect.support then level = old.level
					else level = st:ProcessGemLevel(gd) end
				end
				gem = newGemInstance(gd, level, tonumber(c.quality) or (old and not gd.grantedEffect.support and old.quality) or 0, c.enabled)
			elseif old then
				gem = copyGem(old)
				if c.level ~= nil then gem.level = tonumber(c.level) end
				if c.quality ~= nil then gem.quality = tonumber(c.quality) end
				if c.enabled ~= nil then gem.enabled = c.enabled == true end
			else
				error("gem change on empty slot " .. slot .. " of group " .. gi .. " needs gem", 0)
			end
			if slot > #g.gemList then g.gemList[#g.gemList + 1] = gem else g.gemList[slot] = gem end
		end
	end
	table.sort(removals, function(a, b) return a.slot > b.slot end)
	for _, r in ipairs(removals) do table.remove(r.g.gemList, r.slot) end
	for k, v in pairs(groupEnabled or {}) do
		local g = list[tonumber(k)]
		if not g then error("unknown group " .. tostring(k), 0) end
		touch(g)
		g.enabled = v == true
	end
	for g in pairs(undo.touched) do st:ProcessSocketGroup(g) end
	return undo
end

local function undoGemChanges(undo)
	if not undo then return end
	local st = B().skillsTab
	local list = st.socketGroupList
	for i = #list, 1, -1 do list[i] = nil end
	for i, g in ipairs(undo.groupList) do list[i] = g end
	for _, r in ipairs(undo.groups) do
		for i = #r.g.gemList, 1, -1 do r.g.gemList[i] = nil end
		for i, gem in ipairs(r.gems) do r.g.gemList[i] = gem end
		r.g.enabled = r.enabled
		r.g.mainActiveSkill = r.mas
		if undo.touched[r.g] and isValueInArray(list, r.g) then st:ProcessSocketGroup(r.g) end
	end
end

-- ---------------------------------------------------------------- skill classification
local function damagingTypes(t)
	return has(t, "Damage") or has(t, "Attack") or has(t, "DamageOverTime") or has(t, "DegenOnlySpellDamage")
end

-- activation class as in corpus-eval extract.lua / one-button lib.lua
local function classify(as, inMeta)
	local t = as.skillTypes or {}
	local ge = as.activeEffect.grantedEffect
	local gt = ge.skillTypes or {}
	local f = as.skillFlags or {}
	local function h(n) return has(t, n) or has(gt, n) end
	if h("Triggered") or h("InbuiltTrigger") or (inMeta and not isMetaGemEffect(ge)) then return "triggered" end
	if h("Persistent") or h("HasReservation") then return "persistent" end
	if h("Minion") or h("CreatesMinion") or h("SummonsTotem") or h("Trapped") or h("RemoteMined") or f.totem or f.trap or f.mine or f.minion then return "proxy" end
	return "manual"
end

local function proxyKinds(as)
	local t, gt, f = as.skillTypes or {}, as.activeEffect.grantedEffect.skillTypes or {}, as.skillFlags or {}
	local function h(n) return has(t, n) or has(gt, n) end
	local pk = arr({})
	if h("Minion") or h("CreatesMinion") or f.minion then pk[#pk + 1] = "minion" end
	if h("SummonsTotem") or f.totem then pk[#pk + 1] = "totem" end
	if h("Trapped") or f.trap then pk[#pk + 1] = "trap" end
	if h("RemoteMined") or f.mine then pk[#pk + 1] = "mine" end
	return pk
end

-- active skills of the env, grouped: { [groupIndex] = { as1, as2, ... } } in PoB's mainActiveSkill order
local function skillsByGroup(env)
	local list = B().skillsTab.socketGroupList
	local gidx = {}
	for i, g in ipairs(list) do gidx[g] = i end
	local by = {}
	for _, as in ipairs(env.player.activeSkillList or {}) do
		local gi = as.socketGroup and gidx[as.socketGroup]
		if gi then
			by[gi] = by[gi] or {}
			by[gi][#by[gi] + 1] = as
		end
	end
	return by
end

local function skillName(as)
	local ok, nm = pcall(B().calcsTab.calcs.getActiveSkillDisplayName, as)
	return (ok and nm) or as.activeEffect.grantedEffect.name
end

-- where an item/node-granted group comes from: "item", "node", "mechanic" (PoB synthesises groups
-- such as Explode/Thorns to show their damage), or nil for a player socket group
local function groupGrantKind(g)
	if not g.source then return nil end
	local src = tostring(g.source)
	if g.sourceItem or src:match("^Item:") then return "item" end
	if g.sourceNode or src:match("^Tree:") then return "node" end
	return "mechanic"
end

local function groupSupportCount(g)
	local n = 0
	for _, gem in ipairs(g.gemList or {}) do
		local ge = gem.gemData and gem.gemData.grantedEffect
		if ge and ge.support and gem.enabled ~= false then n = n + 1 end
	end
	return n
end

local function skillFacts(as, gi, si)
	local g = B().skillsTab.socketGroupList[gi]
	local ge = as.activeEffect.grantedEffect
	local gd = as.activeEffect.gemData
	local inMeta = groupMeta(g) ~= nil
	local t = as.skillTypes or {}
	local damaging = damagingTypes(t) or damagingTypes(ge.skillTypes) or false
	local movement = has(t, "Travel") or has(t, "Blink") or false
	local src = as.activeEffect.srcInstance
	local gemSlot
	for i, gem in ipairs(g.gemList) do if gem == src then gemSlot = i end end
	return {
		group = gi, index = si, name = skillName(as), id = ge.id, gemSlot = gemSlot,
		types = typeNames(t),
		activation = classify(as, inMeta), proxyKind = proxyKinds(as),
		damaging = damaging, spell = has(t, "Spell"), attack = has(t, "Attack"),
		movement = movement, utility = (not damaging) and not movement,
		meta = isMetaGemEffect(ge) or false,
		itemGranted = g.source ~= nil,
		grantKind = groupGrantKind(g),
		groupSupports = groupSupportCount(g),
		weaponDefault = (gd and gd.Tier == 0 and ((gd.variantId or ""):find("^PlayerDefault") ~= nil or gd.weaponRequirements ~= nil)) or false,
		enabled = g.enabled ~= false and (not src or src.enabled ~= false),
		triggeredBy = as.triggeredBy and as.triggeredBy.grantedEffect and as.triggeredBy.grantedEffect.name or nil,
	}
end

-- ---------------------------------------------------------------- stats extraction
-- Same keys as tools/corpus-eval/src/extract.lua (STAT_KEYS), then exilegen extensions.
local STAT_KEYS = {
	"CombinedDPS", "TotalDPS", "FullDPS", "TotalDot", "TotalDotDPS", "WithDotDPS", "WithBleedDPS", "WithIgniteDPS", "WithPoisonDPS",
	"BleedDPS", "IgniteDPS", "PoisonDPS", "TotalPoisonDPS", "DecayDPS", "ImpaleDPS", "CullingDPS", "ReservationDPS", "MirageDPS",
	"AverageHit", "AverageDamage", "AverageBurstDamage", "Speed", "HitSpeed", "HitChance", "CritChance", "PreEffectiveCritChance", "CritMultiplier",
	"Life", "LifeUnreserved", "EnergyShield", "Mana", "ManaUnreserved", "Spirit", "SpiritReserved", "SpiritUnreserved",
	"Armour", "Evasion", "Ward", "EnergyShieldRecharge", "LifeRegenRecovery", "ManaRegenRecovery",
	"BlockChance", "EffectiveBlockChance", "SpellBlockChance", "EffectiveSpellBlockChance", "SpellSuppressionChance",
	"MeleeEvadeChance", "ProjectileEvadeChance", "EvadeChance", "PhysicalDamageReduction", "DeflectChance", "EffectiveDeflectChance",
	"FireResist", "ColdResist", "LightningResist", "ChaosResist",
	"FireResistOverCap", "ColdResistOverCap", "LightningResistOverCap", "ChaosResistOverCap",
	"TotalEHP", "PhysicalMaximumHitTaken", "FireMaximumHitTaken", "ColdMaximumHitTaken", "LightningMaximumHitTaken", "ChaosMaximumHitTaken",
	"MovementSpeedMod", "Str", "Dex", "Int", "ManaCost", "ManaPerSecondCost", "CharmLimit",
	"PowerChargesMax", "FrenzyChargesMax", "EnduranceChargesMax",
	-- exilegen additions (not in the corpus records)
	"FireResistMax", "ColdResistMax", "LightningResistMax", "ChaosResistMax",
	"ReqStr", "ReqDex", "ReqInt", "LifeCost", "LifePerSecondCost", "ESCost", "ESPerSecondCost",
	"LifeLeechRate", "ManaLeechRate", "LifeLeechGainRate", "ManaLeechGainRate", "NetLifeRegen", "NetManaRegen",
	"AilmentThreshold", "StunThreshold", "ShockChance", "IgniteChance", "FreezeChance", "ChillChance", "ElectrocuteChance",
	"BleedChance", "PoisonChance", "EnemyShockChance", "FreezeBuildup", "FreezeDurationMod",
	"SkillTriggerRate", "EffectiveSourceRate", "TriggerRateCap", "ManaRegen", "LifeRegen", "ExtraPoints",
}
local function pickStats(o)
	local s = obj({})
	for _, k in ipairs(STAT_KEYS) do
		local v = num(o[k])
		if v then s[k] = v end
	end
	local dt = obj({})
	for _, t in ipairs({ "Physical", "Lightning", "Cold", "Fire", "Chaos" }) do
		local v = o[t .. "HitAverage"]
		if type(v) ~= "number" then
			local mh, oh = o.MainHand and o.MainHand[t .. "HitAverage"], o.OffHand and o.OffHand[t .. "HitAverage"]
			if type(mh) == "number" and type(oh) == "number" then v = (mh + oh) / 2 else v = (type(mh) == "number" and mh) or (type(oh) == "number" and oh) or nil end
		end
		if type(v) == "number" and v > 0 then dt[t] = v end
	end
	s.HitDamageByType = dt
	if s.CombinedDPS and s.TotalDPS then s.NonHitDPS = math.max(0, s.CombinedDPS - s.TotalDPS) end
	return s
end

local function mainSkillInfo(ms)
	if not ms then return nil end
	local ge = ms.activeEffect and ms.activeEffect.grantedEffect
	local flags = arr({})
	for k, v in pairs(ms.skillFlags or {}) do if v == true then flags[#flags + 1] = k end end
	table.sort(flags)
	return {
		name = skillName(ms), id = ge and ge.id, types = typeNames(ms.skillTypes), flags = flags,
		minion = ms.minion and ms.minion.minionData and ms.minion.minionData.name or nil,
	}
end

local function dpsOf(o)
	local p, m = n0(o.CombinedDPS), n0(o.Minion and o.Minion.CombinedDPS)
	return math.max(p, m), p, m
end

-- hit-rate and ailment facts of the calculated main skill of an output
local function offenceFacts(o)
	local avgHit = n0(o.AverageHit)
	local hitChance = num(o.HitChance) or 100
	local hps
	if avgHit > 0 and n0(o.TotalDPS) > 0 then hps = n0(o.TotalDPS) / avgHit else hps = n0(o.Speed) * hitChance / 100 end
	local function pct(k) local v = num(o[k]); return v and math.min(v, 100) or 0 end
	return {
		speed = n0(o.Speed), hitSpeed = num(o.HitSpeed), hitChance = hitChance, hitsPerSec = hps,
		averageHit = avgHit, critChance = pct("CritChance"), critMultiplier = num(o.CritMultiplier),
		shockChance = pct("ShockChance"), igniteChance = pct("IgniteChance"), freezeChance = pct("FreezeChance"),
		chillChance = pct("ChillChance"), electrocuteChance = pct("ElectrocuteChance"),
		bleedChance = pct("BleedChance"), poisonChance = pct("PoisonChance"),
		skillTriggerRate = num(o.SkillTriggerRate), effectiveSourceRate = num(o.EffectiveSourceRate), triggerRateCap = num(o.TriggerRateCap),
	}
end

local function sustainFacts(o)
	local manaPS = n0(o.ManaPerSecondCost)
	local lifePS = n0(o.LifePerSecondCost)
	local esPS = n0(o.ESPerSecondCost)
	-- cost per second when the skill is used continuously (PerSecondCost covers channel/upkeep skills)
	local speed = n0(o.Speed)
	local manaUse = manaPS > 0 and manaPS or n0(o.ManaCost) * speed
	local lifeUse = lifePS > 0 and lifePS or n0(o.LifeCost) * speed
	local manaIn = n0(o.ManaRegenRecovery) + n0(o.ManaLeechGainRate)
	local lifeIn = n0(o.LifeRegenRecovery) + n0(o.LifeLeechGainRate)
	return {
		manaCost = n0(o.ManaCost), lifeCost = n0(o.LifeCost), esCost = n0(o.ESCost),
		manaPerSecondCost = manaPS, lifePerSecondCost = lifePS,
		manaUsePerSec = manaUse, lifeUsePerSec = lifeUse,
		manaRegen = n0(o.ManaRegenRecovery), lifeRegen = n0(o.LifeRegenRecovery),
		manaLeech = n0(o.ManaLeechGainRate), lifeLeech = n0(o.LifeLeechGainRate),
		manaNet = manaIn - manaUse, lifeNet = lifeIn - lifeUse,
		sustainable = (manaIn - manaUse) >= 0 and (lifeUse <= 0 or (lifeIn - lifeUse) >= 0),
	}
end

local function maxHitFacts(o)
	local r = obj({})
	local minV, minT
	for _, t in ipairs({ "Physical", "Fire", "Cold", "Lightning", "Chaos" }) do
		local v = num(o[t .. "MaximumHitTaken"])
		if v then
			r[t] = v
			if not minV or v < minV then minV, minT = v, t end
		end
	end
	r.min, r.minType = minV, minT
	return r
end

local function unsupportedLines(env)
	local ncCount, ncLines = 0, arr({})
	local slots = {}
	for slot in pairs(env.player.itemList or {}) do slots[#slots + 1] = slot end
	table.sort(slots)
	local itemTables = { "implicitModLines", "explicitModLines", "enchantModLines", "buffModLines", "runeModLines", "scourgeModLines", "crucibleModLines" }
	for _, slot in ipairs(slots) do
		local item = env.player.itemList[slot]
		for _, key in ipairs(itemTables) do
			for _, ml in ipairs(item and item[key] or {}) do
				if ml.extra and not ml.disabled and (not item.CheckModLineVariant or item:CheckModLineVariant(ml))
					and not (itemLib and itemLib.isZeroValueLine and itemLib.isZeroValueLine(ml.line)) then
					ncCount = ncCount + 1
					if #ncLines < 40 then ncLines[#ncLines + 1] = ml.line end
				end
			end
		end
	end
	for _, node in pairs(env.allocNodes or {}) do
		for i, line in ipairs(node.sd or {}) do
			local mod = node.mods and node.mods[i]
			if mod and line ~= " " and (mod.extra or not mod.list) then
				ncCount = ncCount + 1
				if #ncLines < 40 then ncLines[#ncLines + 1] = line end
			end
		end
	end
	return { count = ncCount, sample = ncLines }
end

-- ---------------------------------------------------------------- meta-gem energy facts
local function constStat(ge, name)
	local ss = ge.statSets and ge.statSets[1]
	for _, cs in ipairs(ss and ss.constantStats or {}) do if cs[1] == name then return cs[2] end end
	return 0
end
local function levelStat(ge, name, level)
	local ss = ge.statSets and ge.statSets[1]
	if not ss then return 0 end
	for i, s in ipairs(ss.stats or {}) do
		if s == name then
			local lv = ss.levels and (ss.levels[math.min(level or 1, #ss.levels)] or ss.levels[#ss.levels])
			return lv and lv[i] or 0
		end
	end
	return constStat(ge, name)
end
local function qualStat(ge, name, q)
	for _, qs in ipairs(ge.qualityStats or {}) do if qs[1] == name then return qs[2] * (q or 0) end end
	return 0
end
local function energyStatList(ge, level)
	local out = arr({})
	local ss = ge.statSets and ge.statSets[1]
	for _, cs in ipairs(ss and ss.constantStats or {}) do
		if tostring(cs[1]):find("energy") then out[#out + 1] = { stat = cs[1], value = cs[2] } end
	end
	for i, s in ipairs(ss and ss.stats or {}) do
		if tostring(s):find("energy") then out[#out + 1] = { stat = s, value = levelStat(ge, s, level), perLevel = true } end
	end
	return out
end

-- raw energy facts of one meta group (no correction applied)
local function metaGroupFacts(gi)
	local g = B().skillsTab.socketGroupList[gi]
	local mg, mge = groupMeta(g)
	if not mg then return nil end
	local gen = levelStat(mge, "energy_generated_+%", mg.level) + qualStat(mge, "energy_generated_+%", mg.quality)
	local refund = 0
	local spells, supports = arr({}), arr({})
	local maxE = 0
	for slot, gem in ipairs(g.gemList) do
		local ge = gem.gemData and gem.gemData.grantedEffect
		if ge and gem.enabled ~= false then
			if ge.support then
				local sg = constStat(ge, "energy_generated_+%") + levelStat(ge, "energy_generated_+%", gem.level)
				local sr = constStat(ge, "trigger_skills_refund_half_energy_spent_chance_%")
				gen, refund = gen + sg, refund + sr
				if sg ~= 0 or sr ~= 0 then supports[#supports + 1] = { slot = slot, id = ge.id, name = gem.gemData.name, gen = sg, refund = sr } end
			elseif not isMetaGemEffect(ge) then
				local isSpell = has(ge.skillTypes, "Spell")
				local ct = ge.castTime or 0
				if isSpell then maxE = maxE + ct * 100 end
				spells[#spells + 1] = { slot = slot, gemId = gem.gemData.id, id = ge.id, name = gem.gemData.name, castTime = ct, spell = isSpell, level = gem.level }
			end
		end
	end
	local perEvent = obj({})
	local ss = mge.statSets and mge.statSets[1]
	for _, cs in ipairs(ss and ss.constantStats or {}) do
		local ev = tostring(cs[1]):match("centienergy_per_monster_power_on_([%a_]+)$")
		if ev then perEvent[ev] = cs[2] / 100 end
	end
	return {
		group = gi, metaGemId = mg.gemData.id, metaSkillId = mge.id, metaName = mg.gemData.name, metaLevel = mg.level, metaQuality = mg.quality,
		spells = spells, energySupports = supports,
		maxEnergy = maxE, genBonus = gen, refund = refund,
		perEvent = perEvent, perEventUnit = "energy per monster power per event",
		energyStats = energyStatList(mge, mg.level),
	}
end

-- ---------------------------------------------------------------- core evaluation
E.CORE_FIELDS = { "mainSkill", "stats", "minionStats", "main", "dps", "offence", "sustain", "movementSpeed",
	"ailmentThreshold", "spirit", "maxHit", "requirements", "keystones", "uniques" }
-- "marginals" is never implied by "all": it costs one calculation per support of the scored groups
E.ALL_FIELDS = { "mainSkill", "stats", "minionStats", "main", "pobMain", "dps", "offence", "sustain", "movementSpeed",
	"ailmentThreshold", "spirit", "maxHit", "requirements", "keystones", "uniques", "unsupported", "skills", "energy" }

-- per-skill calculations for every enabled damaging active skill (plus meta groups' spells)
local function perSkill(baseOverride, env0)
	local list = B().skillsTab.socketGroupList
	local by = skillsByGroup(env0)
	local out = arr({})
	for gi, skills in pairs(by) do
		local g = list[gi]
		for si, as in ipairs(skills) do
			local f = skillFacts(as, gi, si)
			if (f.damaging or #f.proxyKind > 0) and f.enabled and not f.meta then
				local saved = g.mainActiveSkill
				g.mainActiveSkill = si
				local ov = {}
				for k, v in pairs(baseOverride or {}) do ov[k] = v end
				ov.mainSocketGroup = gi
				local ok, env, o = pcall(calcWith, ov, true)
				g.mainActiveSkill = saved
				if ok then
					local d, p, m = dpsOf(o)
					f.dps, f.playerDps, f.minionDps = d, p, m
					f.offence = offenceFacts(o)
					if o.Minion then f.minionOffence = offenceFacts(o.Minion) end
					local s = sustainFacts(o)
					f.manaUsePerSec, f.lifeUsePerSec = s.manaUsePerSec, s.lifeUsePerSec
					f.types = typeNames(env.player.mainSkill and env.player.mainSkill.skillTypes)
				else
					f.err = tostring(env)
				end
				out[#out + 1] = f
			end
		end
	end
	table.sort(out, function(a, b) if a.group ~= b.group then return a.group < b.group end return a.index < b.index end)
	return out
end

local function energyFacts(skills, mainSel)
	local list = B().skillsTab.socketGroupList
	local metaGroups = {}
	for gi, g in ipairs(list) do if g.enabled ~= false and groupMeta(g) then metaGroups[gi] = true end end
	-- feeder: the selected main skill when it is a manual skill outside meta groups, else the
	-- highest-DPS enabled manual damaging skill outside meta groups
	local feeder
	for _, s in ipairs(skills) do
		if not metaGroups[s.group] and s.activation == "manual" and not s.err then
			if mainSel and s.group == mainSel.group and s.index == mainSel.skill then feeder = s break end
			if not feeder or (s.dps or 0) > (feeder.dps or 0) then feeder = s end
		end
	end
	local res = arr({})
	local gis = {}
	for gi in pairs(metaGroups) do gis[#gis + 1] = gi end
	table.sort(gis)
	for _, gi in ipairs(gis) do
		local e = metaGroupFacts(gi)
		if e then
			e.feeder = feeder and {
				group = feeder.group, index = feeder.index, name = feeder.name, id = feeder.id, dps = feeder.dps,
				hitsPerSec = feeder.offence.hitsPerSec, speed = feeder.offence.speed,
				critChance = feeder.offence.critChance, shockChance = feeder.offence.shockChance,
				igniteChance = feeder.offence.igniteChance, freezeChance = feeder.offence.freezeChance,
				chillChance = feeder.offence.chillChance, electrocuteChance = feeder.offence.electrocuteChance,
				-- hits/s x chance (PoB's per-hit chance; freeze is PoB's FreezeChance, not a buildup model)
				critsPerSec = feeder.offence.hitsPerSec * feeder.offence.critChance / 100,
				shocksPerSec = feeder.offence.hitsPerSec * feeder.offence.shockChance / 100,
				ignitesPerSec = feeder.offence.hitsPerSec * feeder.offence.igniteChance / 100,
				freezesPerSec = feeder.offence.hitsPerSec * feeder.offence.freezeChance / 100,
			} or NULL
			e.triggered = arr({})
			for _, s in ipairs(skills) do
				if s.group == gi and not s.err then
					e.triggered[#e.triggered + 1] = { index = s.index, name = s.name, id = s.id, dps = s.dps,
						pobRate = s.offence.speed, skillTriggerRate = s.offence.skillTriggerRate,
						effectiveSourceRate = s.offence.effectiveSourceRate, triggerRateCap = s.offence.triggerRateCap }
				end
			end
			res[#res + 1] = e
		end
	end
	return res
end

-- DPS of one group's active skills (sum over its non-meta damaging/proxy skills) under override
local function groupDps(gi, override)
	local list = B().skillsTab.socketGroupList
	local g = list[gi]
	local ov = {}
	for k, v in pairs(override or {}) do ov[k] = v end
	ov.mainSocketGroup = gi
	local env0 = calcWith(ov, true)
	local tot = 0
	for si, as in ipairs(skillsByGroup(env0)[gi] or {}) do
		local f = skillFacts(as, gi, si)
		if (f.damaging or #f.proxyKind > 0) and f.enabled and not f.meta then
			local saved = g.mainActiveSkill
			g.mainActiveSkill = si
			local ok, _, o = pcall(calcWith, ov, true)
			g.mainActiveSkill = saved
			if ok then tot = tot + dpsOf(o) end
		end
	end
	return tot
end

-- drop-one support marginals for the main group and every enabled meta (trigger) group
function E.marginals(override, main, baseDps)
	local list = B().skillsTab.socketGroupList
	local groups = { main.group }
	for gi, g in ipairs(list) do if gi ~= main.group and g.enabled ~= false and groupMeta(g) then groups[#groups + 1] = gi end end
	local function mainDps(ov0)
		local ov = {}
		for k, v in pairs(ov0 or {}) do ov[k] = v end
		ov.mainSocketGroup = main.group
		local g = list[main.group]
		local saved = g and g.mainActiveSkill
		if g then g.mainActiveSkill = main.skill or saved end
		local ok, _, o = pcall(calcWith, ov, true)
		if g then g.mainActiveSkill = saved end
		return ok and dpsOf(o) or nil
	end
	local base = mainDps(override)
	local out = arr({})
	for _, gi in ipairs(groups) do
		local g = list[gi]
		if g and not g.source then
			local isMeta = groupMeta(g) ~= nil
			local gBase = isMeta and groupDps(gi, override) or nil
			for slot, gem in ipairs(g.gemList) do
				local ge = gem.gemData and gem.gemData.grantedEffect
				if ge and ge.support and gem.enabled ~= false then
					local undo = applyGemChanges({ { group = gi, slot = slot, enabled = false } })
					local okm, dw = pcall(mainDps, override)
					local okg, gw = true, nil
					if isMeta then okg, gw = pcall(groupDps, gi, override) end
					undoGemChanges(undo)
					out[#out + 1] = { group = gi, slot = slot, gem_id = ge.id, gemId = gem.gemData.id, name = gem.gemData.name,
						dps_without = okm and dw or NULL, dps_base = base or NULL,
						group_dps_without = (isMeta and okg) and gw or nil, group_dps_base = gBase }
				end
			end
		end
	end
	return out
end

-- Evaluate the current live state (plus node override). want: set of fields.
local function evaluate(override, want, opts)
	opts = opts or {}
	local b = B()
	local main = S.main or { group = b.mainSocketGroup or 1, skill = 1 }
	local list = b.skillsTab.socketGroupList
	local ov = {}
	for k, v in pairs(override or {}) do ov[k] = v end
	ov.mainSocketGroup = math.min(math.max(main.group or 1, 1), math.max(#list, 1))
	local g = list[ov.mainSocketGroup]
	local savedMas = g and g.mainActiveSkill
	if g then g.mainActiveSkill = main.skill or savedMas or 1 end
	local ok, env, o = pcall(calcWith, ov, opts.skip_ehp == true)
	if g then g.mainActiveSkill = savedMas end
	if not ok then error(env, 0) end
	local r = obj({ profile = S.profile or NULL })
	local d, p, m = dpsOf(o)
	if want.mainSkill then r.mainSkill = mainSkillInfo(env.player.mainSkill) or NULL end
	if want.stats then
		r.stats = pickStats(o)
		-- PoB fills CharmLimit only in breakdown (MAIN) passes
		if r.stats.CharmLimit == nil and env.player.modDB then
			local mdb = env.player.modDB
			r.stats.CharmLimit = math.min(mdb:Override(nil, "CharmLimit") or mdb:Sum("BASE", nil, "CharmLimit"), 3)
		end
	end
	if want.minionStats then r.minionStats = o.Minion and pickStats(o.Minion) or NULL end
	if want.main then
		r.main = { group = ov.mainSocketGroup, skill = main.skill or 1, selection = S.mainSel or "auto",
			name = env.player.mainSkill and skillName(env.player.mainSkill) or NULL }
	end
	if want.pobMain then
		local pg = S.pobMain or b.mainSocketGroup
		local pgr = list[pg or 1]
		r.pobMain = { group = pg or NULL, skill = pgr and pgr.mainActiveSkill or NULL }
	end
	if want.dps then r.dps = { best = d, player = p, minion = m, full = num(o.FullDPS) } end
	if want.offence then
		r.offence = offenceFacts(o)
		if o.Minion then r.offence.minion = offenceFacts(o.Minion) end
	end
	if want.sustain then r.sustain = sustainFacts(o) end
	if want.movementSpeed then r.movementSpeed = num(o.MovementSpeedMod) or NULL end
	if want.ailmentThreshold then r.ailmentThreshold = { ailment = num(o.AilmentThreshold) or NULL, stun = num(o.StunThreshold) or NULL } end
	if want.spirit then r.spirit = { total = n0(o.Spirit), reserved = n0(o.SpiritReserved), unreserved = n0(o.SpiritUnreserved) } end
	if want.maxHit then r.maxHit = maxHitFacts(o) end
	if want.requirements then
		local req, deficit = obj({}), 0
		for _, a in ipairs({ "Str", "Dex", "Int" }) do
			local need, have = n0(o["Req" .. a]), n0(o[a])
			req[a] = { need = need, have = have }
			if need > have then deficit = deficit + need - have end
		end
		req.deficit = deficit
		r.requirements = req
	end
	if want.keystones or want.uniques then
		local ks, us = arr({}), arr({})
		for id, node in pairs(env.allocNodes or {}) do
			if node.type == "Keystone" then ks[#ks + 1] = { id = id, name = node.dn or node.name, granted = (env.grantedPassives or {})[id] or nil } end
		end
		table.sort(ks, function(a, c) return a.name < c.name end)
		local slots = {}
		for slot in pairs(env.player.itemList or {}) do slots[#slots + 1] = slot end
		table.sort(slots)
		for _, slot in ipairs(slots) do
			local item = env.player.itemList[slot]
			if item and (item.rarity == "UNIQUE" or item.rarity == "RELIC") then us[#us + 1] = { name = item.title or item.name, slot = slot, base = item.baseName } end
		end
		if want.keystones then r.keystones = ks end
		if want.uniques then r.uniques = us end
	end
	if want.unsupported then r.unsupported = unsupportedLines(env) end
	if want.marginals then r.marginals = E.marginals(override, main, d) end
	if want.skills or want.energy then
		local skills = perSkill(override, env)
		if want.skills then r.skills = skills end
		if want.energy then r.energy = energyFacts(skills, main) end
	end
	return r
end
E.evaluate = evaluate

-- ---------------------------------------------------------------- main skill selection
local function selectMain(sel)
	local b = B()
	local list = b.skillsTab.socketGroupList
	S.mainSel = sel or S.mainSel or "auto"
	if S.mainSel == "pob" then
		local gi = S.pobMain or b.mainSocketGroup or 1
		S.main = { group = gi, skill = list[gi] and list[gi].mainActiveSkill or 1 }
	elseif type(S.mainSel) == "number" or tonumber(S.mainSel) then
		local gi = tonumber(S.mainSel)
		S.main = { group = gi, skill = list[gi] and list[gi].mainActiveSkill or 1 }
	elseif type(S.mainSel) == "table" then
		S.main = { group = tonumber(S.mainSel.group) or 1, skill = tonumber(S.mainSel.skill) or 1 }
		S.mainSel = "fixed"
	else
		S.mainSel = "auto"
		local env = calcWith({ mainSocketGroup = b.mainSocketGroup }, true)
		-- highest DPS (player or minion) among skills the player chose: not PoB's synthetic mechanic
		-- groups, and a weapon's default attack only when the player socketed supports into it
		local best, fallback
		for _, s in ipairs(perSkill(nil, env)) do
			if not s.err and (s.dps or 0) > 0 and s.grantKind ~= "mechanic" then
				if s.weaponDefault and s.groupSupports == 0 and s.group ~= S.pobMain then
					if not fallback or s.dps > fallback.dps then fallback = s end
				elseif not best or s.dps > best.dps then best = s end
			end
		end
		best = best or fallback
		if best then S.main = { group = best.group, skill = best.index }
		else S.main = { group = S.pobMain or b.mainSocketGroup or 1, skill = (list[S.pobMain or 1] or {}).mainActiveSkill or 1 } end
	end
	-- PoB's own marker follows the selection, so exports open on the scored skill
	local g = list[S.main.group]
	if g then
		b.mainSocketGroup = S.main.group
		g.mainActiveSkill = S.main.skill
	end
	S.dirty = true
	return S.main
end

-- ---------------------------------------------------------------- profiles
local function applyProfile(name)
	local prof = E.profiles[name]
	if not prof then error("unknown profile " .. tostring(name), 0) end
	local b = B()
	local ct = b.configTab
	if prof.base == "player" then
		ct:SetActiveConfigSet(S.playerConfigSetId, false, true)
	else
		local id = S.profileSets[name]
		if id and ct.configSets[id] then
			ct:SetActiveConfigSet(id, false, true)
		else
			-- fresh PoB-default config set; carry the player's character-state inputs (quest rewards)
			local old = ct.configSets[S.playerConfigSetId]
			local cs = ct:CreateConfigSet(nil, "exg_" .. name)
			for k, v in pairs(old and old.input or {}) do
				if type(k) == "string" then
					for _, pre in ipairs(prof.carry_prefixes or {}) do
						if k:sub(1, #pre) == pre then cs.input[k] = v end
					end
				end
			end
			for k, v in pairs(prof.config or {}) do cs.input[k] = v end
			ct.configSetOrderList[#ct.configSetOrderList + 1] = cs.id
			ct:SetActiveConfigSet(cs.id, false, true)
			S.profileSets[name] = cs.id
		end
	end
	if prof.base == "player" then
		for k, v in pairs(prof.config or {}) do ct.input[k] = v end
	end
	-- twice: list options (enemyIsBoss) set placeholders read by later options
	ct:BuildModList()
	ct:BuildModList()
	S.profile = name
	S.dirty = true
end

function E.profileScoring(name)
	local prof = E.profiles[name or S.profile or ""]
	return prof and prof.scoring or NULL
end

-- ---------------------------------------------------------------- tree
local function nodeKind(node)
	if node.type == "ClassStart" then return "start" end
	if node.type == "AscendClassStart" then return "ascendancy_start" end
	if node.ascendancyName then return node.type == "Notable" and "ascendancy_notable" or "ascendancy" end
	if node.type == "Keystone" then return "keystone" end
	if node.type == "Notable" then return "notable" end
	if node.type == "Socket" then return "jewel" end
	if node.type == "Mastery" then return "mastery" end
	if node.isAttribute then return "attribute" end
	return "small"
end

local function attrChoice(spec, id, node)
	local tn = spec.tree.nodes[id]
	if not (tn and tn.isAttribute) then return nil end
	for i, opt in ipairs(tn.options or {}) do if opt.dn == node.dn then return i end end
	return 0
end

local function nodeFacts(spec, id, node, withLinks)
	local f = { id = id, name = node.dn or node.name, kind = nodeKind(node), type = node.type,
		sd = arr({}), asc = node.ascendancyName or nil, attribute = node.isAttribute or nil }
	for i, l in ipairs(node.sd or {}) do f.sd[i] = l end
	if node.isAttribute then
		local tn = spec.tree.nodes[id]
		f.options = arr({})
		for i, opt in ipairs(tn and tn.options or {}) do f.options[i] = opt.dn end
	end
	if withLinks then
		f.linked = arr({})
		for _, o in ipairs(node.linked or {}) do f.linked[#f.linked + 1] = o.id end
	end
	return f
end

local function pointInfo(env)
	local b = B()
	local spec = b.spec
	local used, ascUsed, secAscUsed, sockets, ws1, ws2 = spec:CountAllocNodes()
	local mo = b.calcsTab.mainOutput or {}
	local extra = env and env.modDB:Sum("BASE", nil, "ExtraPoints") or n0(mo.ExtraPoints)
	local o = mo
	local level = b.characterLevel or 1
	local quest = 0
	for _, a in ipairs(b.acts or {}) do if level >= (a.level or 0) then quest = math.max(quest, a.questPoints or 0) end end
	if level >= 60 then quest = math.max(quest, b.maxWeaponSets or quest) end
	local wsMax = (b.maxWeaponSets or 0) + (env and env.modDB:Sum("BASE", nil, "PassivePointsToWeaponSetPoints") or n0(o.PassivePointsToWeaponSetPoints))
	return {
		used = used, normalUsed = used - math.min(ws1, ws2), ascUsed = ascUsed, secondaryAscUsed = secAscUsed, sockets = sockets,
		weaponSet1 = ws1, weaponSet2 = ws2, weaponSetMax = wsMax,
		available = (level - 1) + quest + extra, levelPoints = level - 1, questPoints = quest, extraPoints = extra,
		ascMax = 8,
	}
end

local function treeView(p)
	local b = B()
	local spec = b.spec
	local env = calcWith({ mainSocketGroup = (S.main and S.main.group) or b.mainSocketGroup }, true)
	local granted = env.grantedPassives or {}
	local maxDist = tonumber(p.max_dist) or 1000
	local withFrontier = p.frontier ~= false
	local nodes = obj({})
	local alloc, asc, ws, attrs, jewels = arr({}), arr({}), obj({}), obj({}), arr({})
	for id, node in pairs(spec.allocNodes) do
		if node.type ~= "ClassStart" and node.type ~= "AscendClassStart" then
			if node.ascendancyName then asc[#asc + 1] = id else alloc[#alloc + 1] = id end
			if (node.allocMode or 0) ~= 0 then ws[tostring(id)] = node.allocMode end
			local c = attrChoice(spec, id, node)
			if c then attrs[tostring(id)] = c end
			nodes[tostring(id)] = nodeFacts(spec, id, node, p.graph)
		end
		if node.type == "Socket" then
			local itemId = spec.jewels and spec.jewels[id]
			local item = itemId and itemId ~= 0 and b.itemsTab.items[itemId]
			jewels[#jewels + 1] = { id = id, item = item and (item.title or item.name) or NULL }
		end
	end
	table.sort(alloc); table.sort(asc)
	local grantedIds = arr({})
	for id, v in pairs(granted) do
		if v then
			grantedIds[#grantedIds + 1] = id
			local node = spec.nodes[id]
			if node and not nodes[tostring(id)] then nodes[tostring(id)] = nodeFacts(spec, id, node, p.graph) end
		end
	end
	table.sort(grantedIds)
	local frontier = arr({})
	if withFrontier then
		for id, node in pairs(spec.nodes) do
			if not node.alloc and node.modKey ~= nil and node.type ~= "ClassStart" and node.type ~= "AscendClassStart"
				and not node.isProxy and (node.pathDist or 1000) <= maxDist and (node.pathDist or 1000) < 1000
				and (p.ascendancy ~= false or not node.ascendancyName) then
				local path = arr({})
				for _, pn in ipairs(node.path or {}) do path[#path + 1] = pn.id end
				local ws = node.pathRoot and (node.pathRoot.allocMode or 0) or 0
				frontier[#frontier + 1] = { id = id, dist = node.pathDist, path = path, granted = granted[id] or nil, ws = ws ~= 0 and ws or nil }
				nodes[tostring(id)] = nodes[tostring(id)] or nodeFacts(spec, id, node, p.graph)
			end
		end
		table.sort(frontier, function(a, c) if a.dist ~= c.dist then return a.dist < c.dist end return a.id < c.id end)
	end
	local r = {
		classId = spec.curClassId, className = spec.curClassName, ascendClassId = spec.curAscendClassId,
		ascendancy = spec.curAscendClassName, startNode = spec.curClass and spec.curClass.startNodeId or NULL,
		treeVersion = spec.treeVersion,
		allocated = alloc, ascendancyNodes = asc, weaponSetNodes = ws, attributeChoices = attrs,
		grantedNodes = grantedIds, jewelSockets = jewels, frontier = frontier, points = pointInfo(env),
	}
	if p.graph then
		-- the whole main tree (not ascendancy) with adjacency, for graph solvers
		for id, node in pairs(spec.nodes) do
			if (not node.ascendancyName or p.ascendancy ~= false) and not node.isProxy and (node.group ~= nil or node.type == "ClassStart") and not nodes[tostring(id)] then
				nodes[tostring(id)] = nodeFacts(spec, id, node, true)
			end
		end
	end
	r.nodes = nodes
	return r
end

local function attrVariant(id, choice)
	S.variants = S.variants or {}
	local key = id * 10 + choice
	if S.variants[key] then return S.variants[key] end
	local spec = B().spec
	local tn = spec.tree.nodes[id]
	if not (tn and tn.isAttribute and tn.options and tn.options[choice]) then error("node " .. id .. " has no attribute option " .. tostring(choice), 0) end
	local nn = copyTableSafe(tn, false, true)
	spec:ReplaceNode(nn, nn.options[choice])
	spec.tree:ProcessStats(nn)
	nn.id = id
	S.variants[key] = nn
	return nn
end

local function parseAdd(e)
	if type(e) == "table" then return tonumber(e.id), tonumber(e.ws or e.weapon_set) end
	return tonumber(e), nil
end

-- The main-tree allocation after removing and adding nodes, with PoB's connectivity rules
-- (PassiveSpec:BuildAllDependsAndPaths prunes nodes that lose their path to the class start;
-- a normal node paths through normal nodes only, a weapon-set node through normal nodes and its
-- own set). Added nodes without an explicit ws get the first mode that connects them: 0, 1, 2.
-- Returns kept {id -> mode}, pruned {ids}. Ascendancy nodes are passed through unchecked.
-- Not modelled: Intuitive-Leap-like jewels and unlock constraints (PoB keeps such nodes).
local function resolveAlloc(addList, removeSet)
	local spec = B().spec
	local mode, cur = {}, {}
	for id, node in pairs(spec.allocNodes) do
		if not node.ascendancyName and node.type ~= "ClassStart" and node.type ~= "AscendClassStart" then
			cur[id] = node.allocMode or 0
			if not removeSet[id] then mode[id] = node.allocMode or 0 end
		end
	end
	local asked = {}
	for _, e in ipairs(addList or {}) do
		local id, ws = parseAdd(e)
		local node = id and spec.nodes[id]
		if not node then error("unknown node " .. tostring(id), 0) end
		if not node.ascendancyName and node.type ~= "ClassStart" and node.type ~= "AscendClassStart" then
			asked[id] = true
			if ws then mode[id] = ws elseif mode[id] == nil then mode[id] = false end
		end
	end
	local starts = {}
	local startId = spec.curClass and spec.curClass.startNodeId
	if startId and spec.nodes[startId] then starts[#starts + 1] = spec.nodes[startId] end
	for socketId, itemId in pairs(spec.jewels or {}) do
		if (mode[socketId] ~= nil) then
			local item = spec:GetJewel(itemId)
			local cn = item and item.jewelData and item.jewelData.alternateClassStart
			local sid = cn and spec.tree.classStartNodeNameMap[cn]
			if sid and spec.nodes[sid] then starts[#starts + 1] = spec.nodes[sid] end
		end
	end
	local kept = {}
	for _, m in ipairs({ 0, 1, 2 }) do
		local seen, q, qi = {}, {}, 1
		for _, sn in ipairs(starts) do q[#q + 1] = sn; seen[sn.id] = true end
		while qi <= #q do
			local u = q[qi]; qi = qi + 1
			if u.type ~= "Mastery" then
				for _, v in ipairs(u.linked or {}) do
					local id = v.id
					local mv = mode[id]
					if mv ~= nil and not seen[id] and not v.ascendancyName then
						local ok
						if mv == false then ok = kept[id] == nil
						elseif mv == 0 then ok = (m == 0) or kept[id] == 0
						else ok = (mv == m) end
						if ok then seen[id] = true; q[#q + 1] = v end
					end
				end
			end
		end
		for id in pairs(seen) do
			if mode[id] == false and kept[id] == nil then kept[id] = m
			elseif mode[id] == m and kept[id] == nil then kept[id] = m end
		end
	end
	local pruned = arr({})
	for id in pairs(mode) do if kept[id] == nil and (cur[id] ~= nil or asked[id]) then pruned[#pruned + 1] = id end end
	table.sort(pruned)
	return kept, cur, pruned
end

local function attrChanges(p)
	local out = {}
	for k, c in pairs(not isnull(p.attr_choices) and p.attr_choices or {}) do
		local id, ch = tonumber(k), tonumber(c)
		if id and ch then out[id] = ch end
	end
	return out
end

-- calc override for a tree what-if; returns override, cleanup(), pruned
local function nodeOverride(p)
	local spec = B().spec
	local addL = not isnull(p.add_nodes) and p.add_nodes or {}
	local remL = not isnull(p.remove_nodes) and p.remove_nodes or {}
	local attrs = attrChanges(p)
	if #addL == 0 and #remL == 0 and next(attrs) == nil then return {}, function() end, nil end
	local removeSet = {}
	for _, e in ipairs(remL) do removeSet[tonumber(e)] = true end
	local kept, cur, pruned = resolveAlloc(addL, removeSet)
	local add, rem = nil, nil
	local restoreModes = {}
	local function addNode(node, m)
		if (node.allocMode or 0) ~= m then restoreModes[node] = node.allocMode; node.allocMode = m end
		add = add or {}; add[node] = true
	end
	for id, m in pairs(kept) do
		local node = spec.nodes[id]
		local c = attrs[id]
		local changeAttr = node.isAttribute and c and (cur[id] == nil or attrChoice(spec, id, node) ~= c)
		if cur[id] == nil then
			addNode(changeAttr and attrVariant(id, c) or node, m)
		elseif changeAttr or cur[id] ~= m then
			rem = rem or {}; rem[node] = true
			addNode(changeAttr and attrVariant(id, c) or copyTableSafe(node, false, true), m)
		end
	end
	for id in pairs(cur) do
		if kept[id] == nil then rem = rem or {}; rem[spec.nodes[id]] = true end
	end
	-- ascendancy nodes: unchecked add / remove
	for _, e in ipairs(addL) do
		local id = parseAdd(e)
		local node = spec.nodes[id]
		if node and node.ascendancyName and not node.alloc then add = add or {}; add[node] = true end
	end
	for _, e in ipairs(remL) do
		local node = spec.nodes[tonumber(e)]
		if node and node.ascendancyName and node.alloc then rem = rem or {}; rem[node] = true end
	end
	return { addNodes = add, removeNodes = rem }, function()
		for node, m in pairs(restoreModes) do node.allocMode = m end
	end, pruned
end

local function applyTree(p)
	local spec = B().spec
	local addL = not isnull(p.add_nodes) and p.add_nodes or {}
	local remL = not isnull(p.remove_nodes) and p.remove_nodes or {}
	local attrs = attrChanges(p)
	if #addL == 0 and #remL == 0 and next(attrs) == nil then return nil end
	local removeSet = {}
	for _, e in ipairs(remL) do removeSet[tonumber(e)] = true end
	local kept, _, pruned = resolveAlloc(addL, removeSet)
	local st = spec:CreateUndoState()
	local list, modes = {}, {}
	for _, id in ipairs(st.hashList) do
		local node = spec.nodes[id]
		if node and (node.ascendancyName or node.type == "ClassStart" or node.type == "AscendClassStart") and not removeSet[id] then
			list[#list + 1] = id
			if (node.allocMode or 0) ~= 0 then modes[id] = node.allocMode end
		end
	end
	for _, e in ipairs(addL) do
		local id = parseAdd(e)
		local node = spec.nodes[id]
		if node and node.ascendancyName and not node.alloc then list[#list + 1] = id end
	end
	for id, m in pairs(kept) do
		list[#list + 1] = id
		if m ~= 0 then modes[id] = m end
	end
	for id in pairs(st.hashOverrides) do
		local tn = spec.tree.nodes[id]
		if tn and tn.isAttribute and kept[id] == nil then st.hashOverrides[id] = nil end
	end
	for id, c in pairs(attrs) do
		local tn = spec.tree.nodes[id]
		if tn and tn.isAttribute and kept[id] ~= nil then
			if not (tn.options and tn.options[c]) then error("node " .. id .. " has no attribute option " .. c, 0) end
			local nn = copyTableSafe(tn, false, true)
			spec:ReplaceNode(nn, nn.options[c])
			spec.tree:ProcessStats(nn)
			st.hashOverrides[id] = nn
		end
	end
	st.hashList, st.weaponSets = list, modes
	E.restoreTree(st)
	return pruned
end

-- restores a spec undo state, first undoing attribute replacements that are not in it
function E.restoreTree(st)
	local spec = B().spec
	for id, node in pairs(spec.nodes) do
		local tn = spec.tree.nodes[id]
		if tn and tn.isAttribute and not st.hashOverrides[id] and node.dn ~= tn.dn then spec:ReplaceNode(node, tn) end
	end
	spec:RestoreUndoState(st)
end

-- ---------------------------------------------------------------- skills view
local function skillsView()
	local b = B()
	local list = b.skillsTab.socketGroupList
	local env = calcWith({ mainSocketGroup = (S.main and S.main.group) or b.mainSocketGroup }, true)
	local by = skillsByGroup(env)
	local groups = arr({})
	for gi, g in ipairs(list) do
		local gems = arr({})
		for slot, gem in ipairs(g.gemList) do
			local gd = gem.gemData
			local ge = gem.grantedEffect or (gd and gd.grantedEffect)
			local req, tier = gemReqLevel(gd)
			gems[#gems + 1] = {
				slot = slot, id = gd and gd.id or NULL, skillId = gem.skillId or (ge and ge.id) or NULL,
				name = (gd and gd.name) or gem.nameSpec or "?", level = gem.level, quality = gem.quality,
				enabled = gem.enabled ~= false, support = (ge and ge.support) and true or false, known = gd ~= nil,
				tags = keysSorted(gd and gd.tags), gemTypes = typeNames(ge and ge.skillTypes),
				attr = gd and ATTR_BY_COLOR[ge and ge.color or 0] or NULL, tier = tier or NULL, reqLevel = req or NULL,
				family = copyArr(ge and ge.gemFamily),
				meta = isMetaGemEffect(ge) or false,
			}
		end
		local skills = arr({})
		for si, as in ipairs(by[gi] or {}) do skills[#skills + 1] = skillFacts(as, gi, si) end
		local src = g.sourceItem
		groups[#groups + 1] = {
			index = gi, label = (g.label and g.label ~= "") and g.label or NULL, slot = g.slot or NULL,
			enabled = g.enabled ~= false, main = S.main and S.main.group == gi or false, pobMain = S.pobMain == gi,
			mainActiveSkill = g.mainActiveSkill or 1, includeInFullDPS = g.includeInFullDPS == true,
			grantedBy = g.source and ((src and (src.title or src.name)) or tostring(g.source)) or NULL,
			metaGroup = groupMeta(g) ~= nil, gems = gems, skills = skills,
		}
	end
	return { groups = groups, main = S.main or NULL, characterLevel = b.characterLevel }
end

-- ---------------------------------------------------------------- valid supports
local function validSupports(p)
	local b = B()
	local list = b.skillsTab.socketGroupList
	local gi = tonumber(p.group)
	local g = gi and list[gi]
	if not g then error("unknown group " .. tostring(p.group), 0) end
	local env = calcWith({ mainSocketGroup = gi }, true)
	local skills = skillsByGroup(env)[gi] or {}
	local si = tonumber(p.skill) or g.mainActiveSkill or 1
	-- support the named active skill; a meta group's supports apply to its socketed spells
	local active = skills[si]
	if active and isMetaGemEffect(active.activeEffect.grantedEffect) then
		for _, as in ipairs(skills) do if not isMetaGemEffect(as.activeEffect.grantedEffect) then active = as break end end
	end
	if not active then return { group = gi, supports = arr({}), err = "no active skill in group" } end
	local level = b.characterLevel or 100
	local usedElsewhere = {}
	for j, g2 in ipairs(list) do
		if j ~= gi and g2.enabled ~= false then
			for _, gem in ipairs(g2.gemList or {}) do
				if gem.gemData and gem.enabled ~= false then usedElsewhere[gem.gemData.id] = j end
			end
		end
	end
	-- current supports in the group, by slot
	local cur = {}
	for slot, gem in ipairs(g.gemList) do
		local ge = gem.gemData and gem.gemData.grantedEffect
		if ge and ge.support and gem.enabled ~= false then cur[#cur + 1] = { slot = slot, gd = gem.gemData } end
	end
	local function conflicts(ga, gb)
		if ga.id == gb.id then return "same" end
		for _, x in ipairs(ga.gemFamily or {}) do for _, y in ipairs(gb.gemFamily or {}) do if x == y then return "family" end end end
		if ga.plusVersionOf == gb.id or gb.plusVersionOf == ga.id then return "lineage" end
		return nil
	end
	local out = arr({})
	for id, gd in pairs(data.gems) do
		local ge = gd.grantedEffect
		if ge and ge.support and not ge.hidden then
			local req, tier = gemReqLevel(gd)
			local okLevel = not (req and req > level)
			local okUse = not usedElsewhere[id] or p.include_used == true
			if okLevel and okUse then
				local ok, s = pcall(calcLib.canGrantedEffectSupportActiveSkill, ge, active)
				if ok and s then
					local cf = arr({})
					local inGroup = false
					for _, c in ipairs(cur) do
						local why = conflicts(ge, c.gd.grantedEffect)
						if why then cf[#cf + 1] = { slot = c.slot, why = why } end
						if c.gd.id == id then inGroup = true end
					end
					out[#out + 1] = {
						id = id, skillId = gd.grantedEffectId, name = gd.name, tier = tier or NULL, reqLevel = req or NULL,
						attr = ATTR_BY_COLOR[ge.color] or NULL, attrCost = 5, socketed = inGroup,
						usedInGroup = usedElsewhere[id] or NULL, conflicts = cf,
						family = copyArr(ge.gemFamily),
						lineage = (gd.name or ""):find("'") ~= nil,
						tags = keysSorted(gd.tags),
					}
				end
			end
		end
	end
	table.sort(out, function(a, c) return a.name < c.name end)
	return { group = gi, skill = si, activeSkill = active.activeEffect.grantedEffect.name, characterLevel = level,
		slots = arr((function() local s = {} for _, c in ipairs(cur) do s[#s + 1] = c.slot end return s end)()),
		supports = out }
end

-- ---------------------------------------------------------------- snapshot / restore
local function snapshot()
	local b = B()
	local list = b.skillsTab.socketGroupList
	local snap = { tree = b.spec:CreateUndoState(), groupList = {}, groups = {}, mainSocketGroup = b.mainSocketGroup,
		main = S.main and { group = S.main.group, skill = S.main.skill } or nil, mainSel = S.mainSel, profile = S.profile,
		configSetId = b.configTab.activeConfigSetId, level = b.characterLevel }
	for i, g in ipairs(list) do
		snap.groupList[i] = g
		local gems = {}
		for j, gem in ipairs(g.gemList) do gems[j] = gem end
		snap.groups[i] = { gems = gems, enabled = g.enabled, mas = g.mainActiveSkill }
	end
	local token = "s" .. S.nextToken
	S.nextToken = S.nextToken + 1
	S.snapshots[token] = snap
	return token
end

local function restore(token)
	local snap = S.snapshots[token]
	if not snap then error("unknown snapshot token " .. tostring(token), 0) end
	local b = B()
	local st = b.skillsTab
	local list = st.socketGroupList
	for i = #list, 1, -1 do list[i] = nil end
	for i, g in ipairs(snap.groupList) do
		list[i] = g
		local s = snap.groups[i]
		for j = #g.gemList, 1, -1 do g.gemList[j] = nil end
		for j, gem in ipairs(s.gems) do g.gemList[j] = gem end
		g.enabled, g.mainActiveSkill = s.enabled, s.mas
		if not g.source then st:ProcessSocketGroup(g) end
	end
	b.mainSocketGroup = snap.mainSocketGroup
	S.main, S.mainSel = snap.main, snap.mainSel
	E.restoreTree(snap.tree)
	if snap.configSetId ~= b.configTab.activeConfigSetId then
		b.configTab:SetActiveConfigSet(snap.configSetId, false, true)
		b.configTab:BuildModList()
		S.profile = snap.profile
	end
	S.dirty = true
	return true
end

-- ---------------------------------------------------------------- ops
local ops = {}
E.ops = ops

local function summary()
	local b = B()
	local spec = b.spec
	local list = b.skillsTab.socketGroupList
	local mg = S.main and list[S.main.group]
	return {
		className = spec.curClassName, ascendancy = spec.curAscendClassName or NULL, level = b.characterLevel,
		treeVersion = spec.treeVersion, targetVersion = b.targetVersion or NULL,
		profile = S.profile or NULL, main = S.main or NULL, mainSelection = S.mainSel,
		pobMainGroup = S.pobMain or NULL, groups = #list,
		mainLabel = mg and (mg.displayLabel or mg.label) or NULL,
		points = pointInfo(),
	}
end

ops._after_load = function(p)
	local b = B()
	E.before_load()
	S.playerConfigSetId = b.configTab.activeConfigSetId
	S.pobMain = b.mainSocketGroup
	S.profile = "as_exported"
	if not isnull(p.profile) and p.profile ~= "as_exported" then applyProfile(p.profile) end
	selectMain(not isnull(p.main) and p.main or "auto")
	return summary()
end

ops.summary = function() return summary() end

ops.set_profile = function(p)
	applyProfile(p.name or p.profile)
	if not isnull(p.main) then selectMain(p.main)
	elseif S.mainSel == "auto" then selectMain("auto") end
	return { profile = S.profile, scoring = E.profileScoring(S.profile), main = S.main }
end

ops.profiles = function()
	return { profiles = E.profiles, current = S.profile or NULL }
end

ops.select_main = function(p)
	local m = selectMain(not isnull(p.main) and p.main or "auto")
	return { main = m, selection = S.mainSel }
end

ops.stats = function(p)
	local want = wantSet(p.fields, { "all" })
	if p.marginals == true then want.marginals = true end
	local r = evaluate(nil, want, p)
	r.scoring = E.profileScoring(S.profile)
	return r
end

ops.skills = function() return skillsView() end
ops.valid_supports = function(p) return validSupports(p) end
ops.tree = function(p) return treeView(p) end

-- what-if: evaluate a change without keeping it
local function whatif(p, defaultFields)
	local want = wantSet(p.fields, defaultFields or { "core" })
	if p.marginals == true then want.marginals = true end
	local undo
	if not isnull(p.gem_changes) or not isnull(p.group_enabled) then
		local ok, u = pcall(applyGemChanges, not isnull(p.gem_changes) and p.gem_changes or nil, not isnull(p.group_enabled) and p.group_enabled or nil)
		if not ok then error(u, 0) end
		undo = u
	end
	local okN, ov, cleanup, pruned = pcall(nodeOverride, p)
	if not okN then undoGemChanges(undo); error(ov, 0) end
	local ok, r = pcall(evaluate, ov, want, p)
	cleanup()
	undoGemChanges(undo)
	if not ok then error(r, 0) end
	if pruned and #pruned > 0 then r.pruned = pruned end
	return r
end

ops.whatif = function(p) return whatif(p) end

ops.whatif_many = function(p)
	local res = arr({})
	for i, v in ipairs(p.variants or {}) do
		if isnull(v.fields) and not isnull(p.fields) then v.fields = p.fields end
		if isnull(v.skip_ehp) and p.skip_ehp ~= nil then v.skip_ehp = p.skip_ehp end
		local ok, r = pcall(whatif, v)
		res[i] = ok and r or { err = tostring(r) }
	end
	return res
end

ops.apply = function(p)
	if not isnull(p.gem_changes) or not isnull(p.group_enabled) then
		applyGemChanges(not isnull(p.gem_changes) and p.gem_changes or nil, not isnull(p.group_enabled) and p.group_enabled or nil)
	end
	local pruned = applyTree(p)
	S.dirty = true
	if p.fields == false then return { applied = true, pruned = pruned } end
	local r = evaluate(nil, wantSet(p.fields, { "core" }), p)
	if pruned and #pruned > 0 then r.pruned = pruned end
	return r
end

ops.snapshot = function() return { token = snapshot() } end
ops.restore = function(p) return { restored = restore(p.token) } end
ops.drop_snapshot = function(p) S.snapshots[p.token] = nil; return { dropped = true } end

ops.export = function()
	local b = B()
	if S.dirty then refresh() end
	local xml = b:SaveDB("code")
	local code = common.base64.encode(Deflate(xml)):gsub("+", "-"):gsub("/", "_")
	return { code = code }
end

ops.export_xml = function()
	if S.dirty then refresh() end
	return { xml = B():SaveDB("code") }
end

ops.lua = function(p)
	if S.dirty then refresh() end
	local f, err = loadstring(p.chunk or p.lua or "", "exg-chunk")
	if not f then error(err, 0) end
	local r = f()
	if type(r) == "string" then local ok, v = pcall(dkjson.decode, r); if ok and v ~= nil then return v end end
	return r
end

ops.gc = function()
	collectgarbage("collect")
	return { mb = math.floor(collectgarbage("count") / 1024) }
end

ops.ping = function() return { ok = true, loaded = B().calcsTab ~= nil and B().calcsTab.mainOutput ~= nil } end

function E.call(reqJson)
	local req = dkjson.decode(reqJson, 1, NULL)
	local fn = ops[req.op]
	if not fn then error("unknown op " .. tostring(req.op), 0) end
	local p = req.p
	if type(p) ~= "table" or p == NULL then p = {} end
	local ok, r = xpcall(function() return fn(p) end, function(e) return debug.traceback(tostring(e), 2) end)
	if not ok then error(r, 0) end
	return dkjson.encode(r, { exception = function() return "null" end })
end

return true
