#!/usr/bin/env python3
"""Builds the localized, indexable copies of the tool pages.

Sources (edit these): index.html, thumbnails.html, repurpose.html (English by default,
the UI still switches EN/RU/UA in the browser).

Generated (never edit by hand):
  ru/index.html, ru/thumbnails.html, ru/repurpose.html   -> /ru/, /ru/thumbnails, /ru/repurpose
  uk/index.html, uk/thumbnails.html, uk/repurpose.html   -> /uk/, /uk/thumbnails, /uk/repurpose
  sitemap.xml (9 URLs with hreflang alternates)

The script also (re)writes two generated blocks inside the English sources:
  <!-- SEO:I18N:START --> ... <!-- SEO:I18N:END -->   in <head>: hreflang, FAQ JSON-LD, URL/lang sync
  <!-- SEO:FAQ:START --> ... <!-- SEO:FAQ:END -->     before <footer>: "How it works" + FAQ in 3 languages

Usage:
  python3 tools/build_i18n_pages.py          # rebuild everything
  python3 tools/build_i18n_pages.py --check  # exit 1 if generated files are out of date (CI)

Standard library only.
"""

import datetime
import html
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = "https://creator-studio-ai-psi.vercel.app"

# UI language code (used by the pages' JS) -> URL prefix / hreflang / og:locale
LANGS = {
    "en": {"prefix": "", "hreflang": "en", "locale": "en_US"},
    "ru": {"prefix": "ru", "hreflang": "ru", "locale": "ru_RU"},
    "ua": {"prefix": "uk", "hreflang": "uk", "locale": "uk_UA"},
}

PAGES = {
    "nicheradar": {"src": "index.html", "slug": ""},
    "thumbnails": {"src": "thumbnails.html", "slug": "thumbnails"},
    "repurpose": {"src": "repurpose.html", "slug": "repurpose"},
}

TOOL_NAMES = {
    "nicheradar": "NicheRadar AI",
    "thumbnails": "Thumbnail & Title Lab",
    "repurpose": "Content Repurposer",
}

# ---------------------------------------------------------------------------
# Localized meta + on-page copy. EN must contain no Cyrillic; UA must not use ы э ъ ё.
# The EN title/description of each page stay as written in the source file.
# ---------------------------------------------------------------------------
META = {
    "nicheradar": {
        "ru": {
            "title": "NicheRadar AI — анализ ниши и топ-5 конкурентов YouTube-канала бесплатно",
            "description": "Вставьте ссылку на YouTube-канал: NicheRadar AI определит нишу, покажет 5 главных конкурентов и предложит 10 идей для вирусных видео. Бесплатно, без регистрации.",
            "keywords": "анализ YouTube-канала, конкуренты на YouTube, как найти нишу на YouTube, идеи для видео",
        },
        "ua": {
            "title": "NicheRadar AI — аналіз ніші та топ-5 конкурентів YouTube-каналу безкоштовно",
            "description": "Вставте посилання на YouTube-канал: NicheRadar AI визначить нішу, покаже 5 головних конкурентів і запропонує 10 ідей для вірусних відео. Безкоштовно, без реєстрації.",
            "keywords": "аналіз YouTube-каналу, конкуренти на YouTube, як знайти нішу на YouTube, ідеї для відео",
        },
    },
    "thumbnails": {
        "ru": {
            "title": "Thumbnail & Title Lab — нейросеть для обложек YouTube 16:9 и A/B-заголовков",
            "description": "Бесплатный генератор превью для YouTube: 3 кликабельные обложки 1280×720 PNG и 6 вариантов заголовка с оценкой CTR. Можно вставить ссылку на видео. Без регистрации.",
            "keywords": "обложка для YouTube, превью для видео, нейросеть для обложек, генератор заголовков YouTube, CTR",
        },
        "ua": {
            "title": "Thumbnail & Title Lab — нейромережа для обкладинок YouTube 16:9 та A/B-заголовків",
            "description": "Безкоштовний генератор прев'ю для YouTube: 3 клікабельні обкладинки 1280×720 PNG і 6 варіантів заголовка з оцінкою CTR. Можна вставити посилання на відео. Без реєстрації.",
            "keywords": "обкладинка для YouTube, прев'ю для відео, нейромережа для обкладинок, генератор заголовків YouTube, CTR",
        },
    },
    "repurpose": {
        "ru": {
            "title": "Content Repurposer — из YouTube-видео пост для Telegram, SEO-статья и посты для соцсетей",
            "description": "Вставьте ссылку на YouTube-видео и получите готовый пост для Telegram-канала, SEO-статью для блога и 5 коротких постов для X и Threads. Бесплатно, без регистрации.",
            "keywords": "видео в статью, пост для Telegram из видео, рерайт видео в текст, контент для соцсетей",
        },
        "ua": {
            "title": "Content Repurposer — з YouTube-відео пост для Telegram, SEO-стаття та пости для соцмереж",
            "description": "Вставте посилання на YouTube-відео й отримайте готовий пост для Telegram-каналу, SEO-статтю для блогу та 5 коротких постів для X і Threads. Безкоштовно, без реєстрації.",
            "keywords": "відео в статтю, пост для Telegram з відео, відео в текст, контент для соцмереж",
        },
    },
}

UI = {
    "en": {"faq": "Frequently asked questions", "more": "More free tools for creators",
           "cd": "CreatorDirectory — 65+ creator tools", "ko": "KoShorts AI Studio — Shorts from any video"},
    "ru": {"faq": "Частые вопросы", "more": "Другие бесплатные инструменты для авторов",
           "cd": "CreatorDirectory — 65+ инструментов для авторов", "ko": "KoShorts AI Studio — Shorts из любого видео"},
    "ua": {"faq": "Часті запитання", "more": "Інші безкоштовні інструменти для авторів",
           "cd": "CreatorDirectory — 65+ інструментів для авторів", "ko": "KoShorts AI Studio — Shorts з будь-якого відео"},
}

COPY = {
    "nicheradar": {
        "en": {
            "h2": "How NicheRadar AI works",
            "intro": "Paste a link to any YouTube channel or video. NicheRadar AI reads the public channel data, describes the niche in plain words, finds the 5 closest competitors with their best videos and suggests 10 video ideas that already work in this niche.",
            "faq": [
                ("Is NicheRadar AI free?", "Yes. The full analysis is free, with no sign-up and no paywall."),
                ("Which link can I paste?", "A channel link (youtube.com/@handle or /channel/...) or a link to any video of the channel. The tool finds the channel automatically."),
                ("How are the competitors chosen?", "NicheRadar AI searches YouTube for the most viewed videos on the topics of your niche and picks the channels behind them that are closest to yours."),
                ("What do I do with the video ideas?", "Every idea comes with a ready title. Open it in Thumbnail & Title Lab in one click to get a 16:9 thumbnail and A/B titles for it."),
            ],
        },
        "ru": {
            "h2": "Как работает NicheRadar AI",
            "intro": "Вставьте ссылку на любой YouTube-канал или видео. NicheRadar AI изучит публичные данные канала, простыми словами опишет нишу, найдёт 5 ближайших конкурентов с их лучшими видео и предложит 10 идей для роликов, которые уже работают в этой нише.",
            "faq": [
                ("NicheRadar AI бесплатный?", "Да. Полный анализ бесплатный, без регистрации и без платных ограничений."),
                ("Какую ссылку можно вставить?", "Ссылку на канал (youtube.com/@имя или /channel/...) или на любое видео канала. Инструмент сам найдёт канал."),
                ("Как подбираются конкуренты?", "NicheRadar AI ищет на YouTube самые просматриваемые видео по темам вашей ниши и выбирает стоящие за ними каналы, которые ближе всего к вашему."),
                ("Что делать с идеями для видео?", "У каждой идеи есть готовое название. Откройте её в Thumbnail & Title Lab в один клик и получите обложку 16:9 и A/B-заголовки."),
            ],
        },
        "ua": {
            "h2": "Як працює NicheRadar AI",
            "intro": "Вставте посилання на будь-який YouTube-канал або відео. NicheRadar AI вивчить публічні дані каналу, простими словами опише нішу, знайде 5 найближчих конкурентів з їхніми найкращими відео та запропонує 10 ідей для роликів, які вже працюють у цій ніші.",
            "faq": [
                ("NicheRadar AI безкоштовний?", "Так. Повний аналіз безкоштовний, без реєстрації та без платних обмежень."),
                ("Яке посилання можна вставити?", "Посилання на канал (youtube.com/@ім'я або /channel/...) або на будь-яке відео каналу. Інструмент сам знайде канал."),
                ("Як добираються конкуренти?", "NicheRadar AI шукає на YouTube найпопулярніші відео за темами вашої ніші та вибирає канали, які за ними стоять і найближчі до вашого."),
                ("Що робити з ідеями для відео?", "Кожна ідея має готову назву. Відкрийте її в Thumbnail & Title Lab в один клік і отримайте обкладинку 16:9 та A/B-заголовки."),
            ],
        },
    },
    "thumbnails": {
        "en": {
            "h2": "How Thumbnail & Title Lab works",
            "intro": "Describe your video in one line or paste a YouTube link. The AI writes 3 thumbnail concepts with different compositions, renders them as 1280×720 PNG files right in your browser and suggests 6 A/B title variants with a CTR score and the trigger behind each one.",
            "faq": [
                ("Is Thumbnail & Title Lab free?", "Yes. It is free, with no sign-up. Every thumbnail downloads as a 1280×720 PNG."),
                ("Can I add my own photo?", "Yes. Upload a photo of yourself or of the product and it is placed into all three compositions."),
                ("What size are the thumbnails?", "1280×720 pixels, 16:9, the size YouTube recommends for custom thumbnails."),
                ("Which languages are supported?", "The interface and the generated text work in English, Russian and Ukrainian. The thumbnail font supports Cyrillic, including the Ukrainian letters."),
            ],
        },
        "ru": {
            "h2": "Как работает Thumbnail & Title Lab",
            "intro": "Опишите видео одной строкой или вставьте ссылку на YouTube. Нейросеть придумает 3 концепции обложки с разными композициями, отрисует их в браузере как PNG 1280×720 и предложит 6 вариантов заголовка для A/B-теста с оценкой CTR и объяснением, на чём он цепляет.",
            "faq": [
                ("Thumbnail & Title Lab бесплатный?", "Да. Бесплатно и без регистрации. Каждая обложка скачивается в PNG 1280×720."),
                ("Можно добавить своё фото?", "Да. Загрузите своё фото или фото товара, и оно встанет во все три композиции."),
                ("Какого размера обложки?", "1280×720 пикселей, 16:9 — размер, который YouTube рекомендует для значков видео."),
                ("Какие языки поддерживаются?", "Интерфейс и тексты работают на английском, русском и украинском. Шрифт обложек поддерживает кириллицу, включая украинские буквы."),
            ],
        },
        "ua": {
            "h2": "Як працює Thumbnail & Title Lab",
            "intro": "Опишіть відео одним рядком або вставте посилання на YouTube. Нейромережа придумає 3 концепції обкладинки з різними композиціями, намалює їх у браузері як PNG 1280×720 і запропонує 6 варіантів заголовка для A/B-тесту з оцінкою CTR і поясненням, чим він чіпляє.",
            "faq": [
                ("Thumbnail & Title Lab безкоштовний?", "Так. Безкоштовно та без реєстрації. Кожна обкладинка завантажується у PNG 1280×720."),
                ("Чи можна додати своє фото?", "Так. Завантажте своє фото або фото товару, і воно з'явиться в усіх трьох композиціях."),
                ("Якого розміру обкладинки?", "1280×720 пікселів, 16:9 — розмір, який YouTube рекомендує для значків відео."),
                ("Які мови підтримуються?", "Інтерфейс і тексти працюють англійською, російською та українською. Шрифт обкладинок підтримує кирилицю, зокрема українські літери."),
            ],
        },
    },
    "repurpose": {
        "en": {
            "h2": "How Content Repurposer works",
            "intro": "Paste a YouTube link, your script or key points. Content Repurposer takes the video subtitles when they are available, understands the content and writes a ready Telegram post, an SEO article for your blog and 5 short posts for X and Threads.",
            "faq": [
                ("Is Content Repurposer free?", "Yes. It is fully free and works without registration."),
                ("What if the video has no subtitles?", "The AI can watch the video itself, or you can paste a transcript, a script or notes into the text field. It works with or without a link."),
                ("Which language is the content written in?", "English, Russian or Ukrainian. Pick the language of the content pack before you start, or leave Auto to follow the video."),
                ("Is the SEO article ready to publish?", "The article comes with a meta description and headings, and you can copy it as Markdown or HTML. Check the facts before you publish it."),
            ],
        },
        "ru": {
            "h2": "Как работает Content Repurposer",
            "intro": "Вставьте ссылку на YouTube, свой сценарий или тезисы. Content Repurposer берёт субтитры видео, если они есть, разбирается в содержании и пишет готовый пост для Telegram-канала, SEO-статью для блога и 5 коротких постов для X и Threads.",
            "faq": [
                ("Content Repurposer бесплатный?", "Да. Полностью бесплатный и работает без регистрации."),
                ("Что если у видео нет субтитров?", "Нейросеть может посмотреть само видео, или вставьте в текстовое поле расшифровку, сценарий или заметки. Работает и со ссылкой, и без неё."),
                ("На каком языке пишется контент?", "На английском, русском или украинском. Выберите язык контент-пакета перед запуском или оставьте «Авто», чтобы использовать язык видео."),
                ("SEO-статью можно сразу публиковать?", "Статья приходит с meta description и подзаголовками, её можно скопировать в Markdown или HTML. Перед публикацией проверьте факты."),
            ],
        },
        "ua": {
            "h2": "Як працює Content Repurposer",
            "intro": "Вставте посилання на YouTube, свій сценарій або тези. Content Repurposer бере субтитри відео, якщо вони є, розбирається у змісті та пише готовий пост для Telegram-каналу, SEO-статтю для блогу та 5 коротких постів для X і Threads.",
            "faq": [
                ("Content Repurposer безкоштовний?", "Так. Повністю безкоштовний і працює без реєстрації."),
                ("Що робити, якщо у відео немає субтитрів?", "Нейромережа може переглянути саме відео, або вставте в текстове поле розшифровку, сценарій чи нотатки. Працює і з посиланням, і без нього."),
                ("Якою мовою пишеться контент?", "Англійською, російською або українською. Виберіть мову контент-пакета перед запуском або залиште «Авто», щоб використати мову відео."),
                ("Чи можна одразу публікувати SEO-статтю?", "Стаття має meta description і підзаголовки, її можна скопіювати в Markdown або HTML. Перед публікацією перевірте факти."),
            ],
        },
    },
}

HEAD_START, HEAD_END = "<!-- SEO:I18N:START -->", "<!-- SEO:I18N:END -->"
FAQ_START, FAQ_END = "<!-- SEO:FAQ:START -->", "<!-- SEO:FAQ:END -->"


def path_for(page, lang):
    prefix = LANGS[lang]["prefix"]
    slug = PAGES[page]["slug"]
    if not prefix:
        return "/" + slug
    return "/" + prefix + "/" + slug


def url_for(page, lang):
    return SITE + path_for(page, lang)


def esc(s):
    return html.escape(s, quote=True)


def head_block(page, lang):
    lines = [HEAD_START]
    for l in LANGS:
        lines.append('<link rel="alternate" hreflang="%s" href="%s" />' % (LANGS[l]["hreflang"], url_for(page, l)))
    lines.append('<link rel="alternate" hreflang="x-default" href="%s" />' % url_for(page, "en"))
    faq = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "inLanguage": LANGS[lang]["hreflang"],
        "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in COPY[page][lang]["faq"]
        ],
    }
    lines.append('<script type="application/ld+json">%s</script>' % json.dumps(faq, ensure_ascii=False).replace("</", "<\\/"))
    page_lang = "null" if lang == "en" else json.dumps(lang)
    # Keeps the URL, <html lang> and the FAQ block in sync with the language picked in the UI.
    lines.append(
        "<script>window.CSI=(function(){var S={nicheradar:\"\",thumbnails:\"thumbnails\",repurpose:\"repurpose\"},"
        "P={en:\"/\",ru:\"/ru/\",ua:\"/uk/\"},page=%s;"
        "function path(p,l){return (P[l]||\"/\")+S[p];}"
        "function key(pn){pn=pn.replace(/\\.html$/,\"\").replace(/^\\/(ru|uk)(\\/|$)/,\"/\").replace(/\\/index$/,\"/\");"
        "pn=pn.replace(/^\\/+|\\/+$/g,\"\");for(var k in S){if(S[k]===pn)return k;}return null;}"
        "function sync(l){try{var want=path(page,l),cur=location.pathname.replace(/\\.html$/,\"\").replace(/\\/index$/,\"/\");"
        "if(cur!==want)history.replaceState(null,\"\",want+location.search+location.hash);}catch(e){}"
        "document.documentElement.lang=l===\"ua\"?\"uk\":l;"
        "var b=document.querySelector(\".seo-faq-in\"),d=document.getElementById(\"seoFaqI18n\");"
        "if(b&&d&&b.getAttribute(\"data-seo-lang\")!==l){try{b.innerHTML=JSON.parse(d.textContent)[l];"
        "b.setAttribute(\"data-seo-lang\",l);b.lang=l===\"ua\"?\"uk\":l;}catch(e){}}"
        "var a=document.querySelectorAll(\"a[href^='/']\");for(var j=0;j<a.length;j++){var u=a[j].getAttribute(\"href\"),q=u.search(/[?#]/),"
        "pn=q<0?u:u.slice(0,q),k=key(pn);if(k&&pn.indexOf(\"/api\")!==0)a[j].setAttribute(\"href\",path(k,l)+(q<0?\"\":u.slice(q)));}}"
        "return {pageLang:%s,path:path,sync:sync};})();</script>" % (json.dumps(page), page_lang)
    )
    lines.append(
        "<style>.seo-faq{max-width:1100px;margin:36px auto 0;padding:0 16px;color:var(--muted);font-size:14px;line-height:1.6}"
        ".seo-faq h2{color:var(--text);font-size:20px;margin:0 0 10px}.seo-faq h3{color:var(--text);font-size:16px;margin:18px 0 8px}"
        ".seo-faq details{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:10px 14px;margin:8px 0}"
        ".seo-faq summary{cursor:pointer;color:var(--text);font-weight:600}.seo-faq details p{margin:8px 0 0}"
        ".seo-faq .more{display:flex;flex-wrap:wrap;gap:8px 18px;margin:8px 0 0;padding:0;list-style:none}"
        ".seo-faq .more a{color:var(--text);border-bottom:1px dashed var(--border)}.seo-faq .more a:hover{border-color:var(--accent2)}</style>"
    )
    lines.append(HEAD_END)
    return "\n".join(lines)


def faq_inner(page, l):
    c, u = COPY[page][l], UI[l]
    out = ["<h2>%s</h2>" % esc(c["h2"]), "<p>%s</p>" % esc(c["intro"]), "<h3>%s</h3>" % esc(u["faq"])]
    for q, a in c["faq"]:
        out.append("<details><summary>%s</summary><p>%s</p></details>" % (esc(q), esc(a)))
    out.append("<h3>%s</h3>" % esc(u["more"]))
    out.append('<ul class="more">')
    for p in PAGES:
        if p != page:
            out.append('<li><a href="%s">%s</a></li>' % (path_for(p, l), esc(TOOL_NAMES[p])))
    out.append('<li><a href="https://creator-directory-chi.vercel.app" target="_blank" rel="noopener">%s</a></li>' % esc(u["cd"]))
    out.append('<li><a href="https://koshorts.vercel.app" target="_blank" rel="noopener">%s</a></li>' % esc(u["ko"]))
    out.append("</ul>")
    return "\n".join(out)


def faq_block(page, lang):
    """Only the page language is in the HTML (no mixed-language text for crawlers);
    the other languages live in a JSON script and are swapped in by CSI.sync()."""
    data = {l: faq_inner(page, l) for l in LANGS}
    return "\n".join([
        FAQ_START,
        '<section class="seo-faq" aria-label="FAQ">',
        '<div class="seo-faq-in" data-seo-lang="%s" lang="%s">' % (lang, LANGS[lang]["hreflang"]),
        data[lang],
        "</div>",
        '<script type="application/json" id="seoFaqI18n">%s</script>'
        % json.dumps(data, ensure_ascii=True).replace("</", "<\\/"),
        "</section>",
        FAQ_END,
    ])


def put_block(src, start, end, block, anchor_re, before):
    """Replace the generated block, or insert it next to anchor_re on first run."""
    if start in src:
        a = src.index(start)
        b = src.index(end) + len(end)
        return src[:a] + block + src[b:]
    m = re.search(anchor_re, src)
    if not m:
        raise SystemExit("anchor %r not found" % anchor_re)
    pos = m.start() if before else m.end()
    return src[:pos] + ("" if before else "\n") + block + ("\n" if before else "") + src[pos:]


def set_attr(src, tag_re, value):
    """Set content="..." / href="..." of the single tag matched by tag_re (group 1 = old value)."""
    head_end = src.index("</head>")
    m = [x for x in re.finditer(tag_re, src) if x.start() < head_end]
    if len(m) != 1:
        raise SystemExit("expected exactly one match for %r, got %d" % (tag_re, len(m)))
    m = m[0]
    return src[: m.start(1)] + value + src[m.end(1):]


def localize(src, page, lang):
    meta = META[page][lang]
    title, desc = meta["title"], meta["description"]
    url = url_for(page, lang)
    s = src
    s = s.replace('<html lang="en">', '<html lang="%s">' % LANGS[lang]["hreflang"], 1)
    s = set_attr(s, r"<title>(.*?)</title>", esc(title))
    s = set_attr(s, r'<meta name="description" content="([^"]*)"', esc(desc))
    s = set_attr(s, r'<link rel="canonical" href="([^"]*)"', url)
    s = set_attr(s, r'<meta property="og:title" content="([^"]*)"', esc(title))
    s = set_attr(s, r'<meta property="og:description" content="([^"]*)"', esc(desc))
    s = set_attr(s, r'<meta property="og:url" content="([^"]*)"', url)
    s = set_attr(s, r'<meta name="twitter:title" content="([^"]*)"', esc(title))
    s = set_attr(s, r'<meta name="twitter:description" content="([^"]*)"', esc(desc))
    others = [LANGS[l]["locale"] for l in LANGS if l != lang]
    s = re.sub(
        r'<meta property="og:locale" content="[^"]*" />\n<meta property="og:locale:alternate" content="[^"]*" />\n<meta property="og:locale:alternate" content="[^"]*" />',
        '<meta property="og:locale" content="%s" />\n<meta property="og:locale:alternate" content="%s" />\n<meta property="og:locale:alternate" content="%s" />'
        % (LANGS[lang]["locale"], others[0], others[1]),
        s, count=1,
    )

    def fix_ld(m):
        data = json.loads(m.group(1))
        if data.get("@type") != "WebApplication":
            return m.group(0)
        data["url"] = url
        data["description"] = desc
        data["inLanguage"] = LANGS[lang]["hreflang"]
        data["keywords"] = meta["keywords"]
        return '<script type="application/ld+json">%s</script>' % json.dumps(data, ensure_ascii=False)

    s = re.sub(r'<script type="application/ld\+json">(\{"@context": "https://schema.org", "@type": "WebApplication".*?)</script>', fix_ld, s, count=1)
    # static internal links -> localized URLs (JS keeps them in sync after a language switch)
    for p in PAGES:
        s = s.replace('href="%s"' % path_for(p, "en"), 'href="%s"' % path_for(p, lang))
        s = s.replace('href="%s?' % path_for(p, "en"), 'href="%s?' % path_for(p, lang))
    s = put_block(s, HEAD_START, HEAD_END, head_block(page, lang), None, False)
    s = put_block(s, FAQ_START, FAQ_END, faq_block(page, lang), None, True)
    s = s.replace("<!DOCTYPE html>", "<!DOCTYPE html>\n<!-- GENERATED by tools/build_i18n_pages.py from %s. Do not edit. -->" % PAGES[page]["src"], 1)
    return s


def sitemap(lastmod):
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">',
    ]
    for p in PAGES:
        for l in LANGS:
            out.append("  <url>")
            out.append("    <loc>%s</loc>" % url_for(p, l))
            for l2 in LANGS:
                out.append('    <xhtml:link rel="alternate" hreflang="%s" href="%s" />' % (LANGS[l2]["hreflang"], url_for(p, l2)))
            out.append('    <xhtml:link rel="alternate" hreflang="x-default" href="%s" />' % url_for(p, "en"))
            out.append("    <lastmod>%s</lastmod>" % lastmod)
            out.append("    <changefreq>weekly</changefreq>")
            out.append("    <priority>%s</priority>" % ("1.0" if (p == "nicheradar" and l == "en") else "0.9" if l == "en" else "0.8"))
            out.append("  </url>")
    out.append("</urlset>")
    return "\n".join(out) + "\n"


def check_purity():
    cyr = re.compile("[\u0400-\u04FF]")
    bad = []
    for page, langs in COPY.items():
        en = json.dumps(langs["en"], ensure_ascii=False)
        if cyr.search(en):
            bad.append("EN copy of %s contains Cyrillic" % page)
        ua = json.dumps([langs["ua"], META[page]["ua"]], ensure_ascii=False)
        if re.search("[ыэъёЫЭЪЁ]", ua):
            bad.append("UA copy of %s contains ы/э/ъ/ё" % page)
        ru = json.dumps([langs["ru"], META[page]["ru"]], ensure_ascii=False)
        if re.search("[іїєґІЇЄҐ]", ru):
            bad.append("RU copy of %s contains Ukrainian letters" % page)
    if cyr.search(json.dumps(UI["en"], ensure_ascii=False)):
        bad.append("EN UI strings contain Cyrillic")
    if bad:
        raise SystemExit("language purity check failed:\n  " + "\n  ".join(bad))


def build():
    check_purity()
    files = {}
    for page, info in PAGES.items():
        src_path = os.path.join(ROOT, info["src"])
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        en = put_block(src, HEAD_START, HEAD_END, head_block(page, "en"),
                       r'<link rel="canonical" href="[^"]*" />', False)
        en = put_block(en, FAQ_START, FAQ_END, faq_block(page, "en"), r"\n<footer>", True)
        files[info["src"]] = en
        for lang in ("ru", "ua"):
            files[os.path.join(LANGS[lang]["prefix"], info["src"])] = localize(en, page, lang)
    return files


def main():
    check = "--check" in sys.argv
    files = build()
    sm_path = os.path.join(ROOT, "sitemap.xml")
    old_sm = open(sm_path, encoding="utf-8").read() if os.path.exists(sm_path) else ""
    m = re.search(r"<lastmod>([^<]+)</lastmod>", old_sm)
    keep = m.group(1) if m else datetime.date.today().isoformat()
    stale = []
    for rel, content in files.items():
        p = os.path.join(ROOT, rel)
        old = open(p, encoding="utf-8").read() if os.path.exists(p) else None
        if old != content:
            stale.append(rel)
    if check:
        if sitemap(keep) != old_sm:
            stale.append("sitemap.xml")
        if stale:
            print("Out of date, run: python3 tools/build_i18n_pages.py\n  " + "\n  ".join(stale))
            sys.exit(1)
        print("Localized pages are up to date.")
        return
    for rel in stale:
        p = os.path.join(ROOT, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(files[rel])
        print("wrote", rel)
    lastmod = datetime.date.today().isoformat() if stale or not old_sm else keep
    new_sm = sitemap(lastmod)
    if new_sm != old_sm:
        with open(sm_path, "w", encoding="utf-8") as f:
            f.write(new_sm)
        print("wrote sitemap.xml")
    if not stale and new_sm == old_sm:
        print("Nothing to do, everything is up to date.")


if __name__ == "__main__":
    main()
