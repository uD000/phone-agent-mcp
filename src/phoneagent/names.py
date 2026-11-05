"""Look-alike names (typos, spacing, accents, Hinglish spellings like kabi/kabhi) for the agent to SHOW the human.

The tool never resolves a recipient by itself. Only an exact name (apart from case and punctuation) counts as a
match; for anything else the agent must ask the human to confirm the suggestion or to give the number.
"""
import re
import unicodedata
from difflib import SequenceMatcher

ASPIRATED = re.compile(r'(b|c|d|g|j|k|p|t)h')


def fold(text):
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().casefold()
    return re.sub(r'[^a-z0-9 ]+', ' ', text)


def tokens(text):
    return [t for t in fold(text).split() if t]


def phonetic(token):
    """Collapse common Indian-English spelling variants so 'kabi' ~ 'kabhi', 'nitish' ~ 'nitesh'."""
    t = ASPIRATED.sub(r'\1', token)
    t = t.replace('w', 'v').replace('z', 'j').replace('ee', 'i').replace('oo', 'u').replace('ou', 'u')
    t = re.sub(r'(.)\1+', r'\1', t)                  # double letters
    t = re.sub(r'y$', 'i', t)
    t = re.sub(r'[aeiou]', lambda m: 'a' if m.group() in 'ae' else ('i' if m.group() in 'iy' else 'u'), t)
    return t


def similarity(a, b):
    raw = SequenceMatcher(None, a, b).ratio()
    pa, pb = phonetic(a), phonetic(b)
    return max(raw, SequenceMatcher(None, pa, pb).ratio() * .97 if pa and pb else 0)


def score(query, name):
    q, n = tokens(query), tokens(name)
    if not q or not n: return 0.0
    per = []
    for qt in q:
        best = 0.0
        for nt in n:
            s = similarity(qt, nt)
            if len(qt) >= 3 and nt.startswith(qt): s = max(s, .9)   # 'adi' -> 'aditi'
            best = max(best, s)
        per.append(best)
    cover = sum(per) / len(per)
    extra = max(0, len(n) - len(q))
    cover *= 1 - .03 * min(extra, 4)                  # name has words the query did not mention
    return round(cover, 3)


def is_exact(query, name):
    """Same name apart from case, accents, punctuation and spacing. Anything else is NOT exact."""
    return ' '.join(tokens(query)) == ' '.join(tokens(name))


def suggest(query, names, limit=5, floor=.55):
    """Ranked look-alikes for the AGENT to show the human. This never decides: it only reports whether a name is an
    exact match (apart from case and punctuation). A typo, a partial name or two similar names is never 'exact'."""
    pool = [(n if isinstance(n, str) else n['name'], n) for n in names]
    ranked = sorted(((score(query, name), name, raw) for name, raw in pool), key=lambda r: -r[0])
    out = []
    for sc, name, raw in ranked:
        if sc < floor and not is_exact(query, name): continue
        out.append(dict(name=name, score=sc, exact=is_exact(query, name)))
        if len(out) >= limit: break
    return out


def canonical(wanted, pool):
    """The saved spelling of an EXACT contact name (apart from case and punctuation). Raises ValueError with the closest
    suggestions for the human otherwise: a typo or partial name is never accepted here."""
    hits = [n for n in dict.fromkeys(pool) if is_exact(wanted, n)]
    if len(hits) == 1: return hits[0]
    if len(hits) > 1: raise ValueError('Several contacts are named %r. Ask the human for the number or which one.' % wanted)
    near = [s['name'] for s in suggest(wanted, list(pool), 3)]
    raise ValueError('%r is not an exact saved contact name.%s Ask the human which one they mean (or for the number).'
                     % (wanted, (' Closest: ' + ', '.join(near) + '.') if near else ' No similar contact is known.'))
