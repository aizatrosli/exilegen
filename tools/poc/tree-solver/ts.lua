-- tree-solver helpers, installed as global TS inside one pob engine.
-- All evaluations are "full set" what-ifs against the current spec via calcFunc{addNodes, removeNodes}.
local dkjson = require("dkjson")
TS = TS or {}
local build = launch.main.modes["BUILD"]
TS.build = build

local function n(v) if type(v) == "number" and v == v and v ~= math.huge and v ~= -math.huge then return v end return nil end

-- metrics from an output table
function TS.metrics(o)
	o = o or {}
	local m = o.Minion or {}
	local dps = n(o.CombinedDPS) or 0
	if dps <= 0 then dps = n(m.CombinedDPS) or 0 end
	if TS.minion then dps = n(m.CombinedDPS) or dps end
	local deficit = 0
	for _, a in ipairs({ "Str", "Dex", "Int" }) do
		local req = n(o["Req" .. a]) or 0
		local have = n(o[a]) or 0
		if req > have then deficit = deficit + (req - have) end
	end
	return {
		dps = dps, ehp = n(o.TotalEHP) or 0, life = n(o.Life) or 0, es = n(o.EnergyShield) or 0,
		fr = n(o.FireResist) or 0, cr = n(o.ColdResist) or 0, lr = n(o.LightningResist) or 0, chr = n(o.ChaosResist) or 0,
		frm = n(o.FireResistMax) or 75, crm = n(o.ColdResistMax) or 75, lrm = n(o.LightningResistMax) or 75,
		str = n(o.Str) or 0, dex = n(o.Dex) or 0, int = n(o.Int) or 0, deficit = deficit,
		spirit = n(o.SpiritUnreserved) or 0,
	}
end

function TS.refresh()
	build.buildFlag = true
	build:OnFrame({})
	TS.calcFunc = build.calcsTab:GetMiscCalculator()
	TS.cache = {}
end

function TS.current()
	return TS.metrics(build.calcsTab.mainOutput)
end

-- attribute variant nodes (not in spec.nodes), keyed id*10+choice
TS.variants = TS.variants or {}
function TS.variant(id, choice)
	local key = id * 10 + choice
	local v = TS.variants[key]
	if v then return v end
	local spec = build.spec
	local tn = spec.tree.nodes[id]
	local newNode = copyTableSafe(tn, false, true)
	spec:ReplaceNode(newNode, newNode.options[choice])
	spec.tree:ProcessStats(newNode)
	newNode.id = id
	TS.variants[key] = newNode
	return newNode
end

-- nodes: array of ids; attrs: map id(string)->choice for attribute nodes; rem: array of ids to remove from spec
function TS.eval(add, attrs, rem)
	local spec = build.spec
	local set = {}
	for _, id in ipairs(add or {}) do
		local node = spec.nodes[id]
		if node then
			local c = attrs and attrs[tostring(id)]
			if node.isAttribute and c then node = TS.variant(id, c) end
			set[node] = true
		end
	end
	local rs = nil
	if rem and #rem > 0 then
		rs = {}
		for _, id in ipairs(rem) do local node = spec.nodes[id]; if node then rs[node] = true; rs[id] = true end end
	end
	local ok, out = pcall(TS.calcFunc, { addNodes = set, removeNodes = rs }, false)
	if not ok then return { err = tostring(out) } end
	return TS.metrics(out)
end

-- batch: list of {add=[...], attrs={...}, rem=[...]}
function TS.evalBatch(jobs)
	local res = {}
	for i, j in ipairs(jobs) do res[i] = TS.eval(j.add, j.attrs, j.rem) end
	return res
end

-- graph export of the main tree (no ascendancy)
function TS.graph()
	local spec = build.spec
	local nodes = {}
	local granted = build.calcsTab.mainEnv and build.calcsTab.mainEnv.grantedPassives or {}
	for id, node in pairs(spec.nodes) do
		if not node.ascendancyName and not node.isProxy and node.group ~= nil or node.type == "ClassStart" then
			local linked = {}
			for _, o in ipairs(node.linked or {}) do
				if not o.ascendancyName then linked[#linked + 1] = o.id end
			end
			nodes[#nodes + 1] = {
				id = id, t = node.type, name = node.dn or node.name, linked = linked,
				attr = node.isAttribute == true, alloc = node.alloc == true, mode = node.allocMode or 0,
				mk = node.modKey or "", granted = granted[id] == true,
				ascStart = node.isAscendancyStart == true, cls = node.classesStart, jewel = node.type == "Socket",
				jewelItem = (spec.jewels and spec.jewels[id] and spec.jewels[id] ~= 0) and true or false,
				override = (spec.hashOverrides and spec.hashOverrides[id] and spec.hashOverrides[id].dn) or nil,
			}
		end
	end
	local asc = {}
	for id, node in pairs(spec.allocNodes) do if node.ascendancyName then asc[#asc + 1] = id end end
	local startId = spec.curClass and spec.curClass.startNodeId
	return { nodes = nodes, asc = asc, start = startId, classId = spec.curClassId, ascId = spec.curAscendClassId,
		secId = spec.curSecondaryAscendClassId, minion = TS.minion or false }
end

-- set the spec to exactly ids (main tree, allocMode 0) + current ascendancy nodes, attrs = id->choice
function TS.setTree(ids, attrs)
	local spec = build.spec
	local list = {}
	for id, node in pairs(spec.allocNodes) do if node.ascendancyName then list[#list + 1] = id end end
	local overrides = {}
	for k, v in pairs(spec.hashOverrides or {}) do
		local tn = spec.tree.nodes[k]
		if not (tn and tn.isAttribute) then overrides[k] = v end
	end
	for _, id in ipairs(ids) do
		list[#list + 1] = id
		local c = attrs and attrs[tostring(id)]
		local tn = spec.tree.nodes[id]
		if tn and tn.isAttribute and c then
			local newNode = copyTableSafe(tn, false, true)
			spec:ReplaceNode(newNode, newNode.options[c])
			spec.tree:ProcessStats(newNode)
			overrides[id] = newNode
		end
	end
	-- restore pristine attribute nodes before import (ReplaceNode mutates spec.nodes in place)
	for id, node in pairs(spec.nodes) do
		local tn = spec.tree.nodes[id]
		if tn and tn.isAttribute and not overrides[id] then spec:ReplaceNode(node, tn) end
	end
	spec:ImportFromNodeList(nil, spec.curClassId, spec.curAscendClassId, spec.curSecondaryAscendClassId, list, {}, overrides, spec.masterySelections or {}, nil)
	TS.refresh()
	local cnt = 0
	for id, node in pairs(spec.allocNodes) do if not node.ascendancyName and node.type ~= "ClassStart" then cnt = cnt + 1 end end
	return { count = cnt, m = TS.current() }
end

-- current tree as {ids, attrs}
function TS.tree()
	local spec = build.spec
	local ids, attrs, modes = {}, {}, {}
	for id, node in pairs(spec.allocNodes) do
		if not node.ascendancyName and node.type ~= "ClassStart" then
			ids[#ids + 1] = id
			if (node.allocMode or 0) > 0 then modes[tostring(id)] = node.allocMode end
			local tn = spec.tree.nodes[id]
			if tn and tn.isAttribute then
				local c = 0
				for i, opt in ipairs(tn.options or {}) do if opt.dn == node.dn then c = i end end
				attrs[tostring(id)] = c
			end
		end
	end
	return { ids = ids, attrs = attrs, modes = modes }
end

TS.enc = function(t) return dkjson.encode(t) end
return dkjson.encode({ ok = true })
