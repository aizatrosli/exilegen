local dkjson = require("dkjson")
local build = launch.main.modes["BUILD"]
local out = {}
for gi, g in ipairs(build.skillsTab.socketGroupList) do
  if g.enabled ~= false then
    for si, as in ipairs(g.displaySkillList or {}) do
      local ge = as.activeEffect and as.activeEffect.grantedEffect
      local st = as.skillTypes or {}
      local function h(n) return SkillType[n] and st[SkillType[n]] end
      local dmg = h("Damage") or h("Attack") or h("DamageOverTime") or h("DegenOnlySpellDamage") or h("Minion") or h("CreatesMinion")
      if ge and dmg then out[#out+1] = { g = gi, s = si, name = ge.name, id = ge.id } end
    end
  end
end
return dkjson.encode({ list = setmetatable(out, {__jsontype="array"}), main = build.mainSocketGroup, mas = (build.skillsTab.socketGroupList[build.mainSocketGroup or 1] or {}).mainActiveSkill or 1 })
