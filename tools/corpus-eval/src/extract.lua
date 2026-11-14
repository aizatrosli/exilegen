-- Evaluated via Engine::eval after a build is loaded and calculated.
-- Returns a JSON string (dkjson) describing stats + tags of the current calc.
-- Read-only: does not change the build.
local dkjson = require("dkjson")
local build = launch.main.modes["BUILD"]
local spec = build.spec
local env = build.calcsTab.mainEnv
local out = build.calcsTab.mainOutput or {}

local function arr(t) return setmetatable(t, { __jsontype = "array" }) end
local function obj(t) return setmetatable(t, { __jsontype = "object" }) end

local STN = {}
for k, v in pairs(SkillType) do STN[v] = k end
local function typeNames(t)
	local r = arr({})
	if t then for k, v in pairs(t) do if v and STN[k] then r[#r + 1] = STN[k] end end end
	table.sort(r)
	return r
end
local function has(t, name) return t and SkillType[name] and t[SkillType[name]] and true or false end

-- ---------------------------------------------------------------- stats
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
}
local function pickStats(o)
	local s = obj({})
	for _, k in ipairs(STAT_KEYS) do
		local v = o[k]
		if type(v) == "number" and v == v and v ~= math.huge and v ~= -math.huge then s[k] = v end
	end
	-- Average hit damage per type (pre-mitigation, per hit), for the damage-type split.
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

local function mainSkillInfo()
	local ms = env and env.player and env.player.mainSkill
	if not ms then return nil end
	local ge = ms.activeEffect and ms.activeEffect.grantedEffect
	local ok, nm = pcall(build.calcsTab.calcs.getActiveSkillDisplayName, ms)
	local flags = arr({})
	for k, v in pairs(ms.skillFlags or {}) do if v == true then flags[#flags + 1] = k end end
	table.sort(flags)
	return {
		name = (ok and nm) or (ge and ge.name) or "?",
		id = ge and ge.id,
		types = typeNames(ms.skillTypes),
		flags = flags,
		minion = ms.minion and ms.minion.minionData and ms.minion.minionData.name or nil,
	}
end

-- ---------------------------------------------------------------- skills
local calculated = {}
for _, as in ipairs(env and env.player and env.player.activeSkillList or {}) do
	local src = as.activeEffect and as.activeEffect.srcInstance
	if src and not calculated[src] then calculated[src] = as end
end

local function classify(types, gtypes, flags, inMetaGroup, isMeta)
	local function h(n) return has(types, n) or has(gtypes, n) end
	if h("Triggered") or h("InbuiltTrigger") or (inMetaGroup and not isMeta) then return "triggered" end
	if h("Persistent") or h("HasReservation") then return "persistent" end
	if h("Minion") or h("CreatesMinion") or h("SummonsTotem") or h("Trapped") or h("RemoteMined")
		or (flags and (flags.totem or flags.trap or flags.mine or flags.minion)) then return "proxy" end
	return "manual"
end

local groups = arr({})
local grantedSkills = arr({})
local manualDamaging, manualDamagingNames, manualSpell = 0, arr({}), false
local itemManualDamaging = arr({})
local activationCounts = obj({ manual = 0, triggered = 0, persistent = 0, proxy = 0 })
for gi, group in ipairs(build.skillsTab.socketGroupList) do
	local inMeta = false
	for _, gem in ipairs(group.gemList) do
		local ge = gem.gemData and gem.gemData.grantedEffect
		if ge and not ge.support and has(ge.skillTypes, "Meta") and has(ge.skillTypes, "Triggers") then inMeta = true end
	end
	local gems = arr({})
	local srcItem = group.sourceItem
	local grantedBy = group.source and ((srcItem and (srcItem.title or srcItem.name)) or tostring(group.source)) or nil
	local groupEnabled = group.enabled ~= false
	for _, gem in ipairs(group.gemList) do
		local gd = gem.gemData
		local ge = gem.grantedEffect or (gd and gd.grantedEffect)
		local e = {
			name = (gd and gd.name) or gem.nameSpec or "?",
			id = gem.skillId or (ge and ge.id),
			level = gem.level, quality = gem.quality,
			enabled = gem.enabled ~= false,
			support = (ge and ge.support) and true or false,
			known = gd ~= nil,
		}
		if ge and not ge.support then
			local calc = calculated[gem]
			local ctypes = calc and calc.skillTypes
			local gtypes = ge.skillTypes
			local isMeta = has(gtypes, "Meta")
			local function h(n) return has(ctypes, n) or has(gtypes, n) end
			e.types = typeNames(ctypes or gtypes)
			e.activation = classify(ctypes, gtypes, calc and calc.skillFlags, inMeta, isMeta)
			local pk = arr({})
			local cf = calc and calc.skillFlags or {}
			if h("Minion") or h("CreatesMinion") or cf.minion then pk[#pk + 1] = "minion" end
			if h("SummonsTotem") or cf.totem then pk[#pk + 1] = "totem" end
			if h("Trapped") or cf.trap then pk[#pk + 1] = "trap" end
			if h("RemoteMined") or cf.mine then pk[#pk + 1] = "mine" end
			e.proxyKind = pk
			e.damaging = h("Damage") or h("Attack") or h("DamageOverTime") or h("DegenOnlySpellDamage") or false
			e.spell = h("Spell")
			e.itemGranted = group.source ~= nil
			e.weaponDefault = (gd and gd.Tier == 0 and ((gd.variantId or ""):find("^PlayerDefault") ~= nil or gd.weaponRequirements ~= nil)) or false
			if groupEnabled and e.enabled and not e.itemGranted and not e.weaponDefault then
				activationCounts[e.activation] = activationCounts[e.activation] + 1
				if e.activation == "manual" then
					if e.damaging then manualDamaging = manualDamaging + 1; manualDamagingNames[#manualDamagingNames + 1] = e.name end
					if e.spell then manualSpell = true end
				end
			end
			-- Item-granted skills the player supports (or plays as main) count too, in a separate tally.
			if groupEnabled and e.enabled and e.itemGranted and not e.weaponDefault and e.activation == "manual" and e.damaging then
				local nsup = 0
				for _, g2 in ipairs(group.gemList) do local ge2 = g2.gemData and g2.gemData.grantedEffect; if ge2 and ge2.support then nsup = nsup + 1 end end
				if nsup > 0 or build.mainSocketGroup == gi then itemManualDamaging[#itemManualDamaging + 1] = e.name end
			end
			if group.source then
				grantedSkills[#grantedSkills + 1] = { name = e.name, source = grantedBy, activation = e.activation }
			end
		end
		gems[#gems + 1] = e
	end
	local active, supports = nil, arr({})
	for _, e in ipairs(gems) do
		if e.support then supports[#supports + 1] = e.name elseif not active then active = e.name end
	end
	groups[#groups + 1] = {
		index = gi, label = group.label ~= "" and group.label or nil, slot = group.slot,
		enabled = groupEnabled, main = build.mainSocketGroup == gi,
		includeInFullDPS = group.includeInFullDPS == true,
		grantedBy = grantedBy, metaGroup = inMeta,
		active = active, supports = supports, gems = gems,
	}
end

-- ---------------------------------------------------------------- tree
local keystones, notables, ascNodes = arr({}), arr({}), arr({})
local nodeCount = 0
for id, node in pairs(spec.allocNodes or {}) do
	nodeCount = nodeCount + 1
	if node.ascendancyName then
		if node.type ~= "AscendClassStart" then ascNodes[#ascNodes + 1] = { id = id, name = node.dn, type = node.type } end
	elseif node.type == "Keystone" then
		keystones[#keystones + 1] = { id = id, name = node.dn }
	elseif node.type == "Notable" then
		notables[#notables + 1] = { id = id, name = node.dn, weaponSet = node.allocMode and node.allocMode ~= 0 and node.allocMode or nil }
	end
end
local byId = function(a, b) return a.id < b.id end
table.sort(keystones, byId); table.sort(notables, byId); table.sort(ascNodes, byId)

-- ---------------------------------------------------------------- items
local uniques, items = arr({}), arr({})
local slots = {}
for slot in pairs(env and env.player and env.player.itemList or {}) do slots[#slots + 1] = slot end
table.sort(slots)
for _, slot in ipairs(slots) do
	local item = env.player.itemList[slot]
	if item then
		local nm = item.title or item.name
		items[#items + 1] = { slot = slot, name = nm, base = item.baseName, rarity = item.rarity }
		if item.rarity == "UNIQUE" or item.rarity == "RELIC" then uniques[#uniques + 1] = nm end
	end
end

-- ------------------------------------------------- unsupported mod lines
local ncCount, ncLines = 0, arr({})
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
for _, node in pairs(env and env.allocNodes or {}) do
	for i, line in ipairs(node.sd or {}) do
		local mod = node.mods and node.mods[i]
		if mod and line ~= " " and (mod.extra or not mod.list) then
			ncCount = ncCount + 1
			if #ncLines < 40 then ncLines[#ncLines + 1] = line end
		end
	end
end

local minion = nil
if out.Minion then minion = pickStats(out.Minion) end

return dkjson.encode({
	className = spec.curClassName,
	ascendancy = spec.curAscendClassName,
	level = build.characterLevel,
	treeVersion = spec.treeVersion,
	targetVersion = build.targetVersion,
	mainSkill = mainSkillInfo(),
	stats = pickStats(out),
	minionStats = minion,
	keystones = keystones, notables = notables, ascendancyNodes = ascNodes, allocatedNodeCount = nodeCount,
	socketGroups = groups,
	activationCounts = activationCounts,
	manualDamagingSkills = manualDamaging,
	manualDamagingSkillNames = manualDamagingNames,
	oneButton = manualDamaging <= 1,
	itemGrantedManualDamagingSkills = itemManualDamaging,
	oneButtonInclItemSkills = (manualDamaging + #itemManualDamaging) <= 1,
	anyManualSpell = manualSpell,
	noCast = not manualSpell,
	grantedSkills = grantedSkills,
	uniques = uniques, items = items,
	unsupportedModLines = ncCount, unsupportedModSample = ncLines,
})
