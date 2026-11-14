-- Commit an assignment {g, slots, ids, mas} into the real socket group.
local dkjson = require("dkjson")
local P = dkjson.decode([==[__PARAMS__]==])
local build = launch.main.modes["BUILD"]
local st = build.skillsTab
local group = st.socketGroupList[tonumber(P.g)]
local out = {}
for k, s in ipairs(P.slots) do
	local id = P.ids[k]
	local gem = group.gemList[s]
	if gem.gemData and gem.gemData.id ~= id then
		local gd = data.gems[id]
		group.gemList[s] = { level = st:ProcessGemLevel(gd), quality = 0, count = 1, enabled = true, enableGlobal1 = true,
			enableGlobal2 = true, gemId = gd.id, nameSpec = gd.name, skillId = gd.grantedEffectId,
			corrupted = false, corruptLevel = 0 }
	end
	out[#out + 1] = data.gems[id].name
end
st:ProcessSocketGroup(group)
group.mainActiveSkill = P.mas or group.mainActiveSkill
build.mainSocketGroup = tonumber(P.g)
return dkjson.encode({ applied = out })
