-- Player's own config (active config set) vs PoB defaults.
local dkjson = require("dkjson")
local ct = launch.main.modes["BUILD"].configTab
local set = ct.configSets[ct.activeConfigSetId]
local nonDefault = setmetatable({}, { __jsontype = "object" })
local n = 0
for var, val in pairs(set.input or {}) do
	if type(val) == "number" or type(val) == "string" or type(val) == "boolean" then
		local d = ct.defaultState[var]
		local isDefault = (val == d) or (d == nil and (val == false or val == 0 or val == ""))
		if not isDefault and var ~= "customMods" then nonDefault[var] = val; n = n + 1 end
	end
end
local customLines = 0
for _, block in ipairs(set.customModsList or {}) do
	if block.enabled ~= false and block.text then
		for line in block.text:gmatch("[^\n]+") do if line:match("%S") then customLines = customLines + 1 end end
	end
end
if customLines == 0 and type(set.input.customMods) == "string" then
	for line in set.input.customMods:gmatch("[^\n]+") do if line:match("%S") then customLines = customLines + 1 end end
end
return dkjson.encode({
	nonDefault = nonDefault, nonDefaultCount = n,
	enemyIsBoss = set.input.enemyIsBoss, customModLines = customLines,
	configSets = #ct.configSetOrderList, activeConfigSetTitle = set.title,
})
