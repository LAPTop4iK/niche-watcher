"""Conservative title match hints across scripts; not semantic proof of relevance."""
import re
import unicodedata

STOP=set('for with and the your app my pro free plus hd lite best simple easy all one new mobile application приложение мобильный'.split())
GENERIC=set('calculator calc tracker timer scanner identifier manager counter converter calendar editor maker assistant rechner zähler калькулятор сканер трекер'.split())


def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKD',value.lower()) if unicodedata.category(c)!='Mn')


def cjk(value):
    return any('\u3040'<=c<='\u30ff' or '\u3400'<=c<='\u9fff' or '\uac00'<=c<='\ud7af' for c in value)


CJK_RUN=re.compile('[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+|[^\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+')
# Grammar of review text in the scanned market languages, casefolded. Without it a
# repeated "nicht", "inte" or "して" passed for a complaint theme (Nov 2026).
REVIEW_STOP=set("""the a an and or but is are was were to of in on for with this that it its i you my me we
they he she at as be by from has have had not no do does did can will would should could very just
so there their about out up down when what which who all more most other some such only own same
than too при этом для что так как это оно они его её мне тебя не и в на с по у же бы очень все
нужно нужна нужен был была были чтобы если когда уже там вот только можно спасибо приложение
even like after been every your because then many also much still want using again make need
anything everything before back used good great please doesn didn isn wasn aren won into them these
those being over here really ever never always keep keeps since though thing things well same apps
application applications what's
nicht aber kann eine einen einem einer eines mehr auch habe hatte sich wenn nach dass noch sehr immer
schon wird werden wurde sind mich jetzt keine kein dann doch ohne oder weil alle alles diese dieser
dieses gibt geht ganz wieder viel viele etwas nichts hier seit durch über leider einfach wirklich
können müssen funktioniert
pour mais avec dans plus sans très tout tous toute toutes cette sont elle nous vous leur même comme
faire fait peut être avoir depuis quand aussi rien encore alors donc cela ceci appli
niet maar voor deze door heel geen wordt worden zijn naar alleen omdat toch echt alles weer steeds
inte appen eller från efter även bara mycket någon något några kunna vara blir finns sedan utan alla
också detta denna
ikke etter også mange noen nogen noget kunne skal være bliver finnes uten uden denne dette meget
veldig
mutta ollut sovellus sovellusta kanssa vain myös joka tämä kuin ennen aina ihan enää
тоже даже после через может будет есть меня него всего этого который которые теперь сейчас просто
вообще приложения приложении
التطبيق البرنامج هذا هذه التي الذي لكن عندما بعد قبل
האפליקציה אפליקציה אבל הזאת הזה שלא אחרי
アプ プリ 沒有 没有 一直 無法 无法 可以 使用 這個 这个 就是 還是 但是 因為 因为 所以 如果 應用 应用
軟體 我們 自己 已經 已经 現在 现在
니다 습니 합니 입니 어요 아요 에서 으로 하는 있는 는데 해서 하고 지만 너무 진짜 정말 어플
""".split())


def theme_word(word):
    """A repeated review word that can name a problem rather than grammar. A bigram
    with hiragana is a Japanese particle, an ending or a word boundary."""
    return word not in REVIEW_STOP and not any('\u3040'<=c<='\u309f' for c in word)


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
    # CJK words run together without spaces, and two characters already make a word.
    matches=sum(t in title_tokens or ((len(t)>=3 or cjk(t)) and t in title) for t in wanted)
    required=min(2,len(wanted))
    return matches>=required
