# Creator Studio AI — launch & indexing kit

Site: https://creator-studio-ai-psi.vercel.app · Sitemap: https://creator-studio-ai-psi.vercel.app/sitemap.xml

Already automated in this repo:

- `robots.txt` + `sitemap.xml` (3 pages), canonical URLs, Open Graph / Twitter cards (`/og/*.png`, 1200×630),
  Schema.org `WebApplication` JSON-LD on every page (free, EN/RU/UK).
- **IndexNow** (Bing, Yandex, Seznam, Naver, Yep): `.github/workflows/indexnow.yml` pings all pages after every
  successful Vercel production deployment. Key file: `/8929cfbb395e30a5a3fa23cccd19a7c0.txt`.
  Manual run: GitHub → Actions → "IndexNow ping" → Run workflow.

Needs your account (cannot be done by a bot — logins, captchas, confirmations):

## 1. Search engines (do first, ~15 min)

| Where | What to do |
| --- | --- |
| Google Search Console — https://search.google.com/search-console | Add property → **URL prefix** → `https://creator-studio-ai-psi.vercel.app/` → method **HTML tag** → send the `content="..."` code to the developer (it is added to all pages) → Verify → Sitemaps → submit `sitemap.xml` → URL inspection → "Request indexing" for `/`, `/thumbnails`, `/repurpose`. |
| Bing Webmaster Tools — https://www.bing.com/webmasters | "Import from Google Search Console" (fastest) or HTML meta tag → submit `sitemap.xml`. IndexNow already works without verification. |
| Yandex Webmaster — https://webmaster.yandex.com | Add site → **Meta tag** → send the code → verify → Indexing → Sitemap files → add `sitemap.xml`. Important for the RU/UA audience. |

## 2. AI-tool directories (backlinks + direct traffic)

Submission rules and prices change — some are free, some have a paid "fast track". Pick the free option.

| Directory | Submit page | Notes |
| --- | --- | --- |
| There's An AI For That | https://theresanaiforthat.com/submit/ | Largest AI catalogue |
| Futurepedia | https://www.futurepedia.io/submit-tool | |
| Toolify | https://www.toolify.ai/submit | |
| TopAI.tools | https://topai.tools/submit | |
| AlternativeTo | https://alternativeto.net/ (Add application) | List as alternative to Canva, TubeBuddy, vidIQ |
| SaaSHub | https://www.saashub.com/ (Submit) | |
| Uneed | https://www.uneed.best/submit-a-tool | Daily launch list |
| Product Hunt | https://www.producthunt.com/posts/new | Launch on Tue–Thu, 00:01 PT; prepare 3–5 screenshots |
| Your own CreatorDirectory | https://creator-directory-chi.vercel.app | Add all 3 tools (dofollow backlink from your domain) |
| KoShorts AI Studio | https://koshorts.vercel.app | Footer link "Creator Studio AI — free YouTube tools" |

Communities (follow each community's self-promotion rules): r/NewTubers, r/youtubers, r/SideProject, Indie Hackers;
RU: vc.ru, Habr (as a "how I built it" article), Telegram chats for YouTubers; UA: DOU, Telegram chats.

## 3. Ready texts

### Suite — EN
- **Name:** Creator Studio AI
- **Tagline (60):** Free AI tools for YouTubers: niche, thumbnails, repurposing
- **Short (160):** Three free AI tools for YouTube creators: find your niche and competitors, generate 1280×720 thumbnails with A/B titles, and turn a video into posts.
- **Long:** Creator Studio AI is a free, no-sign-up toolkit for YouTube creators. NicheRadar AI analyses any channel, defines its niche and shows the top 5 competitors plus 10 viral video ideas. Thumbnail & Title Lab creates 3 ready 1280×720 thumbnails in three different layouts and 6 A/B title variants with a CTR score — even from a YouTube link. Content Repurposer turns one video into a Telegram post, an SEO article and 5 short social posts using the real transcript. Interface in English, Russian and Ukrainian.
- **Tags:** YouTube, thumbnail maker, AI thumbnails, title generator, niche research, competitor analysis, content repurposing, SEO, Telegram, creators
- **Pricing:** Free

### Suite — RU
- **Слоган:** Бесплатные ИИ-инструменты для ютуберов: ниша, обложки, контент
- **Коротко:** Три бесплатных ИИ-инструмента для YouTube: ниша и конкуренты канала, обложки 1280×720 с A/B-заголовками и переработка видео в посты и статьи.
- **Подробно:** Creator Studio AI — бесплатный набор инструментов для авторов YouTube без регистрации. NicheRadar AI анализирует любой канал, определяет нишу, показывает топ-5 конкурентов и 10 идей для вирусных роликов. Thumbnail & Title Lab делает 3 готовые обложки 1280×720 в разных композициях и 6 вариантов заголовков с оценкой CTR — достаточно вставить ссылку на ролик. Content Repurposer превращает одно видео в пост для Telegram, SEO-статью и 5 коротких постов по реальной расшифровке. Интерфейс на русском, украинском и английском.

### Suite — UA
- **Слоган:** Безкоштовні ШІ-інструменти для ютуберів: ніша, обкладинки, контент
- **Коротко:** Три безкоштовні ШІ-інструменти для YouTube: ніша й конкуренти каналу, обкладинки 1280×720 з A/B-заголовками та перетворення відео на пости і статті.
- **Детально:** Creator Studio AI — безкоштовний набір інструментів для авторів YouTube без реєстрації. NicheRadar AI аналізує будь-який канал, визначає нішу, показує топ-5 конкурентів і 10 ідей для вірусних відео. Thumbnail & Title Lab створює 3 готові обкладинки 1280×720 у різних композиціях і 6 варіантів заголовків з оцінкою CTR — достатньо вставити посилання на відео. Content Repurposer перетворює одне відео на пост для Telegram, SEO-статтю та 5 коротких постів за справжньою розшифровкою. Інтерфейс українською, російською та англійською.

### Per tool (EN, for directories that want one product per entry)
- **NicheRadar AI** — https://creator-studio-ai-psi.vercel.app/ — Paste any YouTube channel: get its niche, the top 5 competitors and 10 viral video ideas. Free, no sign-up.
- **Thumbnail & Title Lab** — https://creator-studio-ai-psi.vercel.app/thumbnails — 3 clickable 1280×720 YouTube thumbnails (3 layouts, your photo, accent word) plus 6 A/B titles with CTR score. Works from a YouTube link.
- **Content Repurposer** — https://creator-studio-ai-psi.vercel.app/repurpose — One YouTube video → Telegram post, SEO article (2500–3500 chars) and 5 short posts for X / Threads, based on the real transcript.

Images for listings: `/og/nicheradar.png`, `/og/thumbnails.png`, `/og/repurpose.png` (1200×630) + screenshots of results.
