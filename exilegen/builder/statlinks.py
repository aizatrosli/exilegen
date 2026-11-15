"""Stat id -> state links. A game stat id names the state, who holds it and the relation in its own words:

    base_self_shock_duration_-%            Shocked, on you, scales
    max_power_charges                      Power Charges, a cap
    critical_bonus_+%_final_while_shocked  Shocked, on you, a payoff while it holds
    base_chance_to_poison_on_hit_%         Poisoned, on the enemy, produced

analyse(stat_id) -> [{"state", "role", "target", "form"}], role one of produces, consumes, payoff_enemy,
payoff_self, cap, scales, blocks, mention; target "enemy" or "self". States use the mechanics graph's token
names where one exists, so the result lines up with mechgraph links. conversions(stat_id) reads damage
conversions and "X damage can inflict Y". Words match on underscore boundaries, so "shockwave" is never Shock.

The rules are patterns over ids, tuned on the catalogue's own entities; keystones carry one opaque flag id each
and get nothing from this (edges.py falls back to PoB tags and text for them).
"""
from __future__ import annotations

import re

B, E_ = r'(?<![a-z0-9])', r'(?![a-z0-9])'
# state -> (default target, base/noun forms, participle/adjective forms, origin)   origin: 'graph' = one of the current tokens
S = {}
def st(name, target, base, adj='', origin='new'):
    S[name] = {'target': target, 'base': base.split('|'), 'adj': [a for a in adj.split('|') if a], 'origin': origin}
g = 'graph'
st('enemy:Shocked', 'enemy', 'shock|shocks', 'shocked|shocking', g)
st('enemy:Frozen', 'enemy', 'freeze|freezes', 'frozen|freezing', g)
st('enemy:Chilled', 'enemy', 'chill|chills', 'chilled|chilling', g)
st('enemy:Ignited', 'enemy', 'ignite|ignites|flammability', 'ignited|igniting', g)
st('enemy:Poisoned', 'enemy', 'poison|poisons', 'poisoned|poisoning', g)
st('enemy:Bleeding', 'enemy', 'bleed|bleeds|blood_loss', 'bleeding', g)
st('enemy:Electrocuted', 'enemy', 'electrocute|electrocutes|electrocution', 'electrocuted|electrocuting', g)
st('enemy:Cursed', 'enemy', 'curse|curses|hex|hexes', 'cursed|hexed', g)
st('enemy:Marked', 'enemy', 'mark|marks', 'marked', g)
st('enemy:Stunned', 'enemy', 'stun|stuns|heavy_stun|heavy_stuns', 'stunned|heavy_stunned|stunning|heavy_stunning', g)
st('enemy:ArmourBroken', 'enemy', 'armour_break|break_armour|full_armour_break|sundered_armour', 'broken_armour|armour_broken|fully_broken_armour|fully_armour_broken', g)
st('enemy:Exposed', 'enemy', 'exposure|fire_exposure|cold_exposure|lightning_exposure|elemental_exposure', 'exposed', g)
st('enemy:Pinned', 'enemy', 'pin|pins', 'pinned|pinning', g)
st('enemy:Withered', 'enemy', 'wither|withers', 'withered', g)
st('enemy:Blinded', 'enemy', 'blind|blinds', 'blinded', g)
st('enemy:Intimidated', 'enemy', 'intimidate|intimidates', 'intimidated', g)
st('charge:Power', 'self', 'power_charge|power_charges', '', g)
st('charge:Frenzy', 'self', 'frenzy_charge|frenzy_charges', '', g)
st('charge:Endurance', 'self', 'endurance_charge|endurance_charges', '', g)
st('rage', 'self', 'rage|centirage', '', g)
st('minion', 'self', 'minion|minions', '', g)
st('corpse', 'self', 'corpse|corpses', '', g)
st('crit', 'self', 'critical_strike|critical_strikes|critical_hit|critical_hits|crit|crits|critical', 'crit_recently', g)
st('life:low', 'self', 'low_life', '', g)
st('life:full', 'self', 'full_life', '', g)
st('mana:low', 'self', 'low_mana', '', g)
st('es', 'self', 'energy_shield', '', g)
st('block', 'self', 'block|blocks', 'blocked|blocking', g)
st('totem', 'self', 'totem|totems', '', g)
st('infusion', 'self', 'infusion|infusions|fire_infusion|cold_infusion|lightning_infusion|elemental_infusion', 'infused', g)
st('remnant', 'self', 'remnant|remnants', '', g)
st('glory', 'self', 'glory', '', g)
st('tailwind', 'self', 'tailwind', '', g)
st('onslaught', 'self', 'onslaught', '', g)
st('mana_spent', 'self', 'mana_spent|mana_cost|mana_costs|spend_mana|skill_mana_cost', '', g)
st('life_spent', 'self', 'life_spent|life_cost|life_costs|spend_life|skill_life_cost|cost_life|costs_life', '', g)
st('presence', 'self', 'presence', '', g)
st('deflect', 'self', 'deflect|deflection|deflection_rating', 'deflected', g)
st('evasion', 'self', 'evasion|evasion_rating', '', g)
st('armour', 'self', 'armour|physical_damage_reduction_rating', '', g)
st('flask', 'self', 'flask|flasks', '', g)
st('charm', 'self', 'charm|charms', '', g)
st('kill', 'self', 'kill|kills', 'killed|killing|slain', g)
st('stat:Str', 'self', 'strength', '', g)
st('stat:Dex', 'self', 'dexterity', '', g)
st('stat:Int', 'self', 'intelligence', '', g)
st('stat:Life', 'self', 'maximum_life', '', g)
st('stat:Mana', 'self', 'maximum_mana', '', g)
st('stat:Spirit', 'self', 'spirit', '', g)
st('trigger', 'self', 'trigger|triggers|cast_on', 'triggered', g)
# ---- states the data shows are common but the current graph does not know
st('enemy:Dazed', 'enemy', 'daze|dazes', 'dazed')
st('enemy:Maimed', 'enemy', 'maim|maims', 'maimed')
st('enemy:Hindered', 'enemy', 'hinder|hinders', 'hindered')
st('enemy:Slowed', 'enemy', 'slow|slows', 'slowed')
st('enemy:Immobilised', 'enemy', 'immobilise|immobilises', 'immobilised')
st('enemy:CritWeakness', 'enemy', 'critical_weakness', '')
st('enemy:Impaled', 'enemy', 'impale|impales', 'impaled')
st('enemy:Wet', 'enemy', 'drench', 'wet|drenched')
st('enemy:Culled', 'enemy', 'cull|culling_strike|culling', 'culled')
st('enemy:KnockedBack', 'enemy', 'knockback|knockbacks', 'knocked_back')
st('enemy:Parried', 'enemy', 'parry', 'parried')
st('enemy:Taunted', 'enemy', 'taunt|taunts', 'taunted')
st('enemy:Ailment', 'enemy', 'ailment|ailments|elemental_ailment|elemental_ailments', '')
st('ground:Jagged', 'enemy', 'jagged_ground', '')
st('ground:Ignited', 'enemy', 'ignited_ground|burning_ground|ground_fire', '')
st('ground:Chilled', 'enemy', 'chilled_ground|chilling_ground', '')
st('ground:Shocked', 'enemy', 'shocked_ground|shocking_ground', '')
st('ground:Consecrated', 'self', 'consecrated_ground', '')
st('combo', 'self', 'combo', '')
st('archon', 'self', 'archon', '')
st('shapeshift', 'self', 'shapeshift', 'shapeshifted')
st('thorns', 'self', 'thorns', '')
st('leech', 'self', 'leech|life_leech|mana_leech', 'leeching')
st('ward', 'self', 'ward|runic_ward', '')
st('guard', 'self', 'guard', '')
st('surge', 'self', 'instilling|instilling_fire|instilling_cold|instilling_lightning|instilling_chaos', '')
st('arcane_surge', 'self', 'arcane_surge', '')
st('ice_crystal', 'self', 'ice_crystal|ice_crystals|frost_wall', '')
st('offering', 'self', 'offering|offerings', '')
st('companion', 'self', 'companion|companions', '')
st('spectre', 'self', 'spectre|spectres', '')
st('hazard', 'self', 'hazard|hazards', '')
st('grenade', 'self', 'grenade|grenades', '')
st('warcry', 'self', 'warcry|warcries', 'warcried')
st('empowered', 'self', 'empower|empowers', 'empowered|exerted')
st('stage', 'self', 'stage|stages', '')
st('seal', 'self', 'seal|seals', '')
st('energy', 'self', 'centienergy|energy_generated|meta_energy', '')
st('heat', 'self', 'heat|crossbow_heat', '')
st('reload', 'self', 'reload|ammo', 'reloading|reloaded')
st('dodge_roll', 'self', 'dodge_roll', '')
st('volatility', 'self', 'volatility', '')
st('herald', 'self', 'herald|heralds', '')
st('banner', 'self', 'banner|banners|valour', '')
st('channel', 'self', 'channel', 'channelling|channelled')
st('aura', 'self', 'aura|auras', '')
st('mana:full', 'self', 'full_mana', '')
st('es:full', 'self', 'full_energy_shield', '')
st('es:low', 'self', 'low_energy_shield', '')
st('shield', 'self', 'shield', '')
st('unarmed', 'self', 'unarmed', '')
st('dual_wield', 'self', 'dual_wielding|dual_wield', '')
for w in 'bow crossbow quarterstaff spear mace axe sword dagger flail wand staff focus'.split():
    st('weapon:' + w, 'self', w + '|' + w + 's' + ('|maces' if w == 'mace' else ''), '')
st('stat:Attributes', 'self', 'attributes|all_attributes', '')

# longer forms must win over shorter ones that they contain (energy_shield vs shield, ignited_ground vs ignited ...)
ALL = []
for name, d in S.items():
    for f in d['base']: ALL.append((f, name, 'base'))
    for f in d['adj']: ALL.append((f, name, 'adj'))
ALL.sort(key=lambda x: -len(x[0]))
BIG = re.compile(B + '(' + '|'.join(re.escape(f) for f, _, _ in ALL) + ')' + E_)
FORM = {}
for f, n, k in ALL: FORM.setdefault(f, (n, k))

KW = {
    'block': 'avoid cannot immune immunity never unaffected prevent prevents without no'.split(),
    'consume': 'consume consumes consumed consuming remove removes removed lose loses lost spend spent spending expend sacrifice sacrifices detonate detonates'.split(),
    'cap': 'max maximum limit'.split(),
    'gate_enemy': 'vs against enemies enemy target targets monsters'.split(),
    'gate_self': 'while per if when with during each on from after have has at reaching whilst for'.split(),
    'produce': 'chance gain gains add adds apply applies inflict inflicts inflicted grant grants create creates spawn spawns generate generates cause causes summon summons always can also recover regenerate'.split(),
    'name': 'trigger triggers triggered by of support skill'.split(),
}
KWMAP = {w: k for k, ws in KW.items() for w in ws}
POST = [
    ('produce', r'^_(?:build_up|(?:buff_|duration_|\w+_enem(?:y|ies)_){0,2}on_(?:hit|kill|crit|critical|block|use|melee|attack|spell|skill|stun|parry|death)|when_hit|chance|buildup|gained|gain|generation|regeneration|generated)'),
    ('consume', r'^_(?:consumed|spent|cost|costs|loss|consumption|is_consumed)'),
    ('scales', r'^_(?:effect|duration|magnitude|potency|amount|rate|damage|speed|area|radius|level|gem_level|life|efficiency|as_though)'),
    ('cap', r'^_(?:limit|allowed|stacks_allowed)|^s?_allowed'),
    ('gate_self', r'^_recently'),
]
POST = [(r, re.compile(p)) for r, p in POST]
RESOURCES = {'rage', 'charge:Power', 'charge:Frenzy', 'charge:Endurance', 'flask', 'charm', 'glory', 'energy', 'combo', 'infusion', 'remnant'}
ENEMYWORD = re.compile(B + r'(?:enemy|enemies|target|targets|vs|against|monsters)' + E_)
SELFWORD = re.compile(B + r'(?:self|on_you|on_yourself|you_are|yourself|shocks_you|when_hit|inflicted_on_you)' + E_)

def analyse(sid: str):
    """-> list of dicts {state, role, target, form}"""
    out = []
    low = sid
    for m in BIG.finditer(low):
        form = m.group(1); state, kind = FORM[form]
        d = S[state]
        pre, post = low[:m.start()], low[m.end():]
        ptoks = [t for t in re.split(r'_+', pre) if t]
        while ptoks and ptoks[0] in ('base', 'local', 'global', 'additional', 'total', 'non', 'unique', 'display', 'virtual', 'x', 'X', '%'): ptoks = ptoks[1:]
        role = None
        # adjective use: "<state>_enemies"
        if kind == 'adj' and re.match(r'^_(?:enem(?:y|ies)|targets?|monsters?)' + E_, post) and d['target'] == 'enemy':
            role = 'payoff_enemy'
        if role is None:
            kws = [(i, KWMAP[t]) for i, t in enumerate(ptoks) if t in KWMAP]
            # only keywords close to the state (within 4 tokens) decide; skill-name words reset
            near = [(i, k) for i, k in kws if len(ptoks) - i <= 5]
            if near:
                i, k = near[-1]
                prev = near[-2][1] if len(near) > 1 else None
                if k == 'name':
                    role = None
                elif k == 'cap':
                    role = 'payoff_self' if prev == 'gate_self' else 'produces' if prev == 'produce' else 'cap'
                elif k == 'produce':
                    role = 'payoff_self' if prev == 'gate_self' and len(ptoks) - i <= 3 and ptoks[i] in ('gain', 'gains', 'add', 'inflict', 'apply', 'applies', 'create') and i > 0 and ptoks[i - 1] in ('you', 'on', 'when', 'if') else 'produces'
                    if prev == 'block' and i > 0 and ptoks[i - 1] in KW['block']: role = 'blocks'
                elif k == 'consume':
                    role = 'blocks' if prev == 'block' and i > 0 and ptoks[i - 1] in ('not', 'cannot', 'never') else 'consumes'
                elif k == 'block': role = 'blocks'
                elif k == 'gate_enemy':
                    role = 'payoff_enemy'
                    if prev in ('consume', 'produce') and len(near) > 1 and near[-2][0] >= i - 2 and ptoks[i] in ('enemy', 'enemies', 'target', 'targets'):
                        role = 'consumes' if prev == 'consume' else 'produces'
                elif k == 'gate_self': role = 'payoff_self'
        if ptoks and ptoks[-1] in ('are', 'have', 'gain', 'gains') and ({'enemies', 'enemy'} & set(ptoks[-5:])) and d['target'] == 'enemy':
            role = 'produces'
        # a resource that regenerates or is generated over time is made, not scaled
        if state in RESOURCES and (re.match(r'^_(?:regeneration|generation|generated|gain)', post)
                                   or {'generate', 'generates'} & set(ptoks)):
            role = 'produces'
        if ptoks[-2:] == ['should', 'have']:
            role = 'produces'
        if role is None and ptoks[-2:] == ['affected', 'by']:
            role = 'payoff_self'; kind = 'adj'
        if role is None and kind == 'base' and d['target'] == 'enemy' and re.match(r'^_(?:enem(?:y|ies)|targets?)' + E_, post):
            role = 'produces'
        if role is None and ptoks[-2:] == ['hit', 'damage'] and re.match(r'^_(?:multiplier|build_up)', post):
            role = 'produces'
        if role is None and not ptoks and kind == 'base' and re.match(r'^_on_', post):
            role = 'produces'
        if role is None and not ptoks and re.match(r'^_(?:have|deal|gain|ignore|are|is|can|cannot|always|never|explode|affect|grant|take|inflict|apply|penetrate|count|you_inflict|from_your)' + E_, post):
            role = 'payoff_self' if state == 'crit' else 'scales'
        if role is None and re.match(r'^_slots?' + E_, post):
            role = 'cap'
        if role is None and kind == 'adj' and re.match(r'^_(?:for|when)_', post) and (not ptoks or ptoks[-1] in ('randomly', 'be', 'become', 'becomes')):
            role = 'produces'; low = low + '_on_you'
        if role is None and state == 'crit' and re.match(r'^_(?:chance|multiplier|damage_bonus|bonus)', post):
            role = 'scales'
        if role is None and re.match(r'^_(?:rating_)?\+?%|^_\+(?:_|$)|^_rating(?:_|$)|^_(?:recharge|delay|regeneration)', post) and not re.match(r'^_%_(?:to|as)', post):
            role = 'scales'
        if role is None:
            for r, rx in POST:
                if rx.search(post):
                    role = r; break
        if role is None and re.match(r'^(?:_\+?%?)?(?:_final)?$|^_\+$', post) and not ptoks:
            role = 'scales'           # bare stat: "strength_+%", "evasion_rating"
        if role is None:
            role = 'mention'
        if role == 'gate_self': role = 'payoff_self'
        if role == 'produce': role = 'produces'
        if role == 'consume': role = 'consumes'
        # "chance_to_not_consume" / "skip_consume"
        if role == 'consumes' and re.search(r'(?:not|skip)_consume', pre + form + post[:10]) : role = 'scales'
        # target
        target = d['target']
        if d['target'] == 'enemy':
            if SELFWORD.search(low): target = 'self'
            elif role == 'payoff_self':
                if kind == 'adj' and not ENEMYWORD.search(low): target = 'self'      # while_shocked, if_frozen
                else: role = 'payoff_enemy'                                           # on_shock, per_poison: event you cause
            elif role == 'blocks' and not re.search(r'never|cannot_(?:shock|freeze|ignite|chill|poison|inflict|apply)', low): target = 'self'
            elif role == 'scales' and re.search(r'on_self|self_|_on_you', low): target = 'self'
        out.append({'state': state, 'role': role, 'target': target, 'form': form})
    # a list of states sharing one suffix ("ignite_shock_chill_duration_+%") inherits the role of the last one
    ms = list(BIG.finditer(low))
    for j in range(len(out) - 2, -1, -1):
        if out[j]['role'] == 'mention' and ms[j].end() + 1 == ms[j + 1].start() and out[j + 1]['role'] != 'mention' and S[out[j]['state']]['target'] == S[out[j + 1]['state']]['target']:
            out[j]['role'] = out[j + 1]['role']; out[j]['target'] = out[j + 1]['target']
    return out

DT = 'physical|fire|cold|lightning|chaos|elemental|all'
CONV = [re.compile(r'(?:^|_)(' + DT + r')?_?damage_%_(?:to_)?(?:convert(?:ed)?_to|gain(?:ed)?_as|as_extra|to_gain_as|to_convert_to)_(?:extra_)?(' + DT + r')'),
        re.compile(r'(?:^|_)(' + DT + r')_damage_%_to_(?:convert_to|gain_as)_(' + DT + r')'),
        re.compile(r'gain_%_of_(' + DT + r')?_?damage_as_extra_(' + DT + r')'),
        re.compile(r'gain_%_(?:of_)?()(' + DT + r')_damage' + E_),
        re.compile(r'()damage_to_gain_as_(' + DT + r')'),
        re.compile(r'(?:^|_)(' + DT + r')?_?damage_%?_?(?:to_gain|gained)_as_(?:extra_)?(phys|' + DT + r')'),
        re.compile(r'(' + DT + r')?_?damage_%_(?:gained_)?as_(?:extra_)?(' + DT + r')(?:_damage)?')]
CANAIL = re.compile(r'(?:^|_)(' + DT + r')_damage_(?:from_hits_)?(?:can|also_can|also_contributes_to|contributes_to)_(?:inflict_)?(\w+)')
AILW = {'bleeding': 'enemy:Bleeding', 'shock': 'enemy:Shocked', 'freeze': 'enemy:Frozen', 'ignite': 'enemy:Ignited', 'chill': 'enemy:Chilled', 'poison': 'enemy:Poisoned',
        'bleed': 'enemy:Bleeding', 'electrocute': 'enemy:Electrocuted', 'flammability': 'enemy:Ignited', 'pin': 'enemy:Pinned', 'stun': 'enemy:Stunned'}
def conversions(sid):
    out = []
    for rx in CONV:
        m = rx.search(sid)
        if m:
            src, dst = m.group(1), m.group(2)
            if re.search(r'taken', sid[:m.end()]): break          # defensive "damage taken as": not an offensive conversion
            dst = 'physical' if dst == 'phys' else dst
            if src and src not in ('elemental', 'all') and src != dst: out.append(('dmg:' + src.capitalize(), 'consumes'))
            for x in (['fire', 'cold', 'lightning'] if src == 'elemental' else []):
                if x != dst: out.append(('dmg:' + x.capitalize(), 'consumes'))
            if dst not in ('elemental', 'all'): out.append(('dmg:' + dst.capitalize(), 'produces'))
            break
    m2 = re.search(r'(?:^|_)(' + DT + r')_damage_(shocks|freezes|ignites|chills|poisons|electrocutes)' + E_, sid)
    if m2:
        if m2.group(1) not in ('elemental', 'all'): out.append(('dmg:' + m2.group(1).capitalize(), 'consumes'))
        out.append((AILW[m2.group(2)[:-1]], 'produces'))
    m = CANAIL.search(sid)
    if m:
        if m.group(1) not in ('elemental', 'all'): out.append(('dmg:' + m.group(1).capitalize(), 'consumes'))
        for x in (['fire', 'cold', 'lightning'] if m.group(1) == 'elemental' else []):
            out.append(('dmg:' + x.capitalize(), 'consumes'))
        for w, tok in AILW.items():
            if re.search(B + w + E_, m.group(2)): out.append((tok, 'produces'))
    return out
