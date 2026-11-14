-- Switch to a fresh config set: PoB defaults (fresh CreateConfigSet), no custom mods.
-- Quest-reward choices are character state, not combat assumptions, so they carry over.
local ct = launch.main.modes["BUILD"].configTab
local old = ct.configSets[ct.activeConfigSetId]
local cs = ct:CreateConfigSet(nil, "corpus_pinnacle_locked")
for k, v in pairs(old.input or {}) do
	if type(k) == "string" and k:sub(1, 5) == "quest" then cs.input[k] = v end
end
ct.configSetOrderList[#ct.configSetOrderList + 1] = cs.id
ct:SetActiveConfigSet(cs.id, false, true)
return cs.id
