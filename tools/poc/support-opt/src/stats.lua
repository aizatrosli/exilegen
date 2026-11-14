local dkjson = require("dkjson")
local build = launch.main.modes["BUILD"]
local o = build.calcsTab.mainOutput or {}
local env = build.calcsTab.mainEnv
local ms = env and env.player and env.player.mainSkill
local nm = nil
if ms then local ok, n = pcall(build.calcsTab.calcs.getActiveSkillDisplayName, ms); nm = ok and n or nil end
local function n(v) if type(v)=="number" and v==v and v~=math.huge and v~=-math.huge then return v end return nil end
local m = o.Minion or {}
return dkjson.encode({ CombinedDPS=n(o.CombinedDPS), TotalDPS=n(o.TotalDPS), TotalDotDPS=n(o.TotalDotDPS), FullDPS=n(o.FullDPS),
  MinionCombinedDPS=n(m.CombinedDPS), MinionTotalDPS=n(m.TotalDPS), Life=n(o.Life), ES=n(o.EnergyShield), TotalEHP=n(o.TotalEHP),
  skill=nm, main=build.mainSocketGroup })
