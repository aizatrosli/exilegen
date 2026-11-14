"""Knowledge base from PoB data: skill/support descriptions + types, unique item text; mechanic tagging."""
import re, os, json, glob
POB = __import__('os').environ['POB_ROOT']  # synced PoB-PoE2 tree (pob-sync --dest)

def load_skills():
    out = {}
    for fn in glob.glob(POB + '/Data/Skills/*.lua'):
        txt = open(fn, encoding='utf-8', errors='replace').read()
        for m in re.finditer(r'skills\["([^"]+)"\] = \{(.*?)\n\tlevels = ', txt, re.S):
            sid, body = m.group(1), m.group(2)
            nm = re.search(r'\n\tname = "([^"]*)"', body)
            ds = re.search(r'\n\tdescription = "((?:[^"\\]|\\.)*)"', body)
            st = re.search(r'\n\tskillTypes = \{([^}]*)\}', body)
            req = re.search(r'\n\trequireSkillTypes = \{([^}]*)\}', body)
            out[sid] = dict(id=sid, name=nm.group(1) if nm else sid, desc=ds.group(1) if ds else '',
                            support='\n\tsupport = true' in body,
                            types=re.findall(r'SkillType\.(\w+)', st.group(1)) if st else [],
                            req=re.findall(r'SkillType\.(\w+)', req.group(1)) if req else [])
    return out

def load_uniques():
    out = {}
    for fn in glob.glob(POB + '/Data/Uniques/*.lua'):
        txt = open(fn, encoding='utf-8', errors='replace').read()
        for blk in re.findall(r'\[\[\n(.*?)\]\]', txt, re.S):
            lines = [l for l in blk.strip().split('\n') if l.strip()]
            if len(lines) < 2: continue
            name, base = lines[0], lines[1]
            body = [re.sub(r'\{[^}]*\}', '', l) for l in lines[2:]
                    if not re.match(r'(Variant|League|Implicits|Source|Requires|Selected Variant|Sockets|LevelReq|Radius|Limited to|Has Alt Variant)', l)]
            out.setdefault(name, dict(name=name, base=base, slot=os.path.basename(fn)[:-4], text='\n'.join(body)))
    return out

MECH = {
    'fire': r'\bfire\b', 'cold': r'\bcold\b', 'lightning': r'\blightning\b', 'chaos': r'\bchaos\b', 'physical': r'\bphysical\b',
    'ignite': r'ignit', 'shock': r'shock', 'freeze': r'freez|frozen', 'chill': r'chill', 'poison': r'poison', 'bleed': r'bleed',
    'crit': r'critical', 'minion': r'minion|companion|summon', 'totem': r'totem', 'trigger': r'trigger|cast on|invocation|energy',
    'conversion': r'convert|as extra|gain .*% of .* damage as|damage as extra', 'projectile': r'projectile', 'curse': r'curse|hex',
    'mark': r'\bmark', 'herald': r'herald', 'remnant': r'remnant', 'charges': r'power charge|frenzy charge|endurance charge',
    'es': r'energy shield', 'mana': r'\bmana\b', 'life': r'\blife\b', 'block': r'\bblock', 'armour': r'armour', 'evasion': r'evasion|deflect',
    'grenade': r'grenade', 'dot': r'damage over time|degen|withered|wither', 'infusion': r'infus', 'exposure': r'exposure|penetrat|resistance.*enem|enem.*resistance',
    'spell': r'\bspell', 'attack': r'\battack', 'aoe': r'area', 'duration': r'duration', 'stun': r'\bstun|heavy stun|daze', 'speed': r'(attack|cast) speed',
    'cooldown': r'cooldown', 'reservation': r'spirit|reserv', 'recoup_leech': r'leech|recoup',
}
TYPEMAP = {'Fire': 'fire', 'Cold': 'cold', 'Lightning': 'lightning', 'Chaos': 'chaos', 'Physical': 'physical', 'Minion': 'minion',
           'CreatesMinion': 'minion', 'Totemable': None, 'SummonsTotem': 'totem', 'Triggerable': None, 'Triggered': 'trigger', 'Meta': 'trigger',
           'Projectile': 'projectile', 'Spell': 'spell', 'Attack': 'attack', 'Area': 'aoe', 'Duration': 'duration', 'DamageOverTime': 'dot',
           'Herald': 'herald', 'Mark': 'mark', 'Curse': 'curse', 'Grenade': 'grenade', 'Cooldown': 'cooldown', 'Persistent': 'reservation',
           'CausesBurning': 'ignite'}

def mech_tags(text, types=()):
    t = text.lower()
    s = {k for k, rx in MECH.items() if re.search(rx, t)}
    for ty in types:
        if TYPEMAP.get(ty): s.add(TYPEMAP[ty])
    return s

def family(name):
    return re.sub(r'\s+(I|II|III|IV|V)$', '', name)
