"""Conservative title match hints across scripts; not semantic proof of relevance."""
import re
import unicodedata

STOP=set('for with and the your app my pro free plus hd lite best simple easy all one new mobile application приложение мобильный'.split())
GENERIC=set('calculator calc tracker timer scanner identifier manager counter converter calendar editor maker assistant rechner zähler калькулятор сканер трекер'.split())


def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKD',value.lower()) if unicodedata.category(c)!='Mn')


def tokens(value):
    return {t for t in re.findall(r'[^\W_]+',normalize(value),re.UNICODE) if len(t)>=2 and t not in STOP}


def relevant(name,query):
    query_tokens=tokens(query)
    distinctive=query_tokens-GENERIC
    # A query containing only a generic tool name carries no topical precision.
    wanted=distinctive or query_tokens
    if not wanted:
        return False
    title=normalize(name);title_tokens=tokens(name)
    matches=sum(t in title_tokens or (len(t)>=3 and t in title) for t in wanted)
    required=min(2,len(wanted))
    return matches>=required
