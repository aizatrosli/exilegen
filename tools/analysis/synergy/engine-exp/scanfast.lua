local dkjson = require("dkjson")
local build = launch.main.modes["BUILD"]
local calcs = build.calcsTab.calcs
local list = build.skillsTab.socketGroupList
local origMain = build.mainSocketGroup
local function n(v) if type(v)=="number" and v==v and v~=math.huge and v~=-math.huge then return v end return nil end
local rows = {}
local seen = {}
for gi, g in ipairs(list) do
  if g.enabled ~= false then
    local origMAS = g.mainActiveSkill
    for si, as in ipairs(g.displaySkillList or {}) do
      local ge = as.activeEffect and as.activeEffect.grantedEffect
      local st = as.skillTypes or {}
      local function h(x) return SkillType[x] and st[SkillType[x]] end
      local dmg = h("Damage") or h("Attack") or h("DamageOverTime") or h("DegenOnlySpellDamage") or h("Minion") or h("CreatesMinion")
      local persist = (h("Persistent") or h("HasReservation")) and not (h("Minion") or h("CreatesMinion"))
      if ge and dmg and not persist then
        build.mainSocketGroup = gi; g.mainActiveSkill = si
        if wipeGlobalCache then wipeGlobalCache() end
        local ok, env = pcall(calcs.buildOutput, build, "MAIN")
        local r = { g = gi, s = si, name = ge.name, id = ge.id }
        if ok and env then
          local o = env.player.output; local m = o.Minion or {}
          r.CombinedDPS = n(o.CombinedDPS); r.TotalDPS = n(o.TotalDPS); r.TotalDotDPS = n(o.TotalDotDPS)
          r.MinionCombinedDPS = n(m.CombinedDPS)
        else r.err = tostring(env) end
        rows[#rows+1] = r
      end
    end
    g.mainActiveSkill = origMAS
  end
end
build.mainSocketGroup = origMain
if wipeGlobalCache then wipeGlobalCache() end
build.calcsTab:BuildOutput()
return dkjson.encode({ main = origMain, rows = setmetatable(rows, {__jsontype="array"}) })
