# Teach Me

Tell it what you want to learn, and Teach Me builds you a day-by-day course: lessons with diagrams, hand-picked YouTube videos, narrated walkthroughs and quizzes. It runs on your own computer as a desktop app, and can also serve itself as a small web site.

- **Any topic, any level.** "How to fix a running toilet", "Excel pivot tables", "The Cold War". Pick a level from pure beginner to expert, or take a short placement quiz and get a course that fills your gaps.
- **Any language.** Courses can be written in English, Spanish, Chinese, Hindi, Arabic, French or any other language you type.
- **Your own material.** Build a course from your own websites and files (Markdown, text, Word, PDF), such as staff training on your own products.
- **You bring the API keys.** Teach Me calls the AI providers with your keys and you pay them directly. A written course typically costs a few cents; checking videos and narration add a little more. With no keys at all it can use a free model running on your computer.

Everything stays on your computer: your keys in `.env`, your courses in `courses/`.

## How it works

A short interview sets up each new course:

1. **What do you want to learn?** A few words is enough.
2. **Tell me more** (optional): your goals, what to focus on, what you already know.
3. **Where should the material come from?** The internet, specific websites (up to 5), or files from your computer (up to 20).
4. **How do you want to learn?** Written lessons, videos only, or a mix of lessons, videos and quizzes.
5. **Your level**, or **Quiz me**: questions from simple to expert, then a course built around what you got wrong.
6. **Language**: pick from the list or type any language.

Teach Me then plans the course and builds it day by day while you watch. You can start reading Day 1 before the rest is finished. Each day can have:

- a lesson with diagrams,
- YouTube videos that an AI model has watched and checked against the lesson, opened at the most useful part,
- an animated walkthrough with narration,
- a quiz.

Progress and quiz scores are saved. A finished course can be saved as a PDF and emailed.

## Setup

You need [Python](https://www.python.org/downloads/) 3.11 or newer and [Node.js](https://nodejs.org) 18 or newer.

```bash
git clone https://github.com/rickfortier/teach-me-anything.git
cd teach-me-anything
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cd electron
npm install
npm start
```

The first run creates `.env` with every setting blank. Add the keys you have there, or click **AI setup** in the app to add a free Gemini key. `.env` is listed in `.gitignore`: never commit it or share it.

## API keys

**To try it with no keys,** install [Ollama](https://ollama.com), run `ollama pull qwen3:8b` (about 5 GB), and add `ollama/auto` to the task lists in `config.yaml`. Lessons are slower and simpler than with a cloud model.

**For good courses, add a free Gemini key.** Open [Google AI Studio](https://aistudio.google.com/apikey) and click *Create API key*. No credit card is needed. The free tier has daily limits, and Google may use free-tier requests to improve its products, so keep personal details out of your topics.

| Key in `.env` | Used for | Where to get it |
|---|---|---|
| `GEMINI_API_KEY` | Course plans, walkthrough drawings, and watching YouTube videos to check them. Free tier available | [aistudio.google.com](https://aistudio.google.com/apikey) |
| `OPENAI_API_KEY` | Lessons and quizzes on the cheapest models | [platform.openai.com](https://platform.openai.com) |
| `OPENROUTER_API_KEY` | Many providers' models behind one key (the default `config.yaml` uses it) | [openrouter.ai](https://openrouter.ai) |
| `ANTHROPIC_API_KEY` | Claude, as a fallback for any task, and web search for videos | [console.anthropic.com](https://console.anthropic.com) |
| `YOUTUBE_API_KEY` | Finding videos (YouTube Data API v3). Without it, video days get the walkthrough only | [console.cloud.google.com](https://console.cloud.google.com) |

Any one of the AI keys is enough to build courses. Each task in `config.yaml` lists up to three models as `provider/model`, tried in order, and a model whose provider has no key is skipped. To see what your keys can run, and why not, use `python teachme.py check` or **AI setup** in the app.

What changes with fewer keys:

- **No Gemini key:** videos are chosen by title and description instead of being watched.
- **No YouTube key:** no real videos, unless you set `video_source: web` in `config.yaml` (uses Claude or OpenRouter web search).
- **One provider only:** list only that provider's models in `config.yaml`. Watching videos still needs Gemini.

### Optional extras

- **Narration.** Walkthroughs are read aloud by [Kokoro](https://github.com/remsky/Kokoro-FastAPI), a free voice model on your computer:
  `docker run -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu:latest` (or the `-gpu` image).
  If it isn't running, your system voice reads the scenes instead. Set `tts_engine: off` in `config.yaml` to turn it off.
- **Email.** Fill in the `SMTP_*` settings to email course PDFs. Set `REPORT_EMAIL` to also receive each new course's build report.

## Courses from your own websites and files

In the interview, choose **Specific websites** and/or **Files on my computer**. Teach Me reads the pages and the pages they link to on the same site (25 by default; it skips cart and account pages and honours robots.txt), and the files you add. Facts about products, prices and policies come only from those sources, with links or page references back. Scanned PDFs (pictures of pages) have no text and are skipped.

From the command line: `python teachme.py create --topic "..." --level pure_beginner --mode docs --source https://example.com --source-file guide.pdf --language Spanish`.

## Running it as a web site

Teach Me can also serve itself to a browser, for example from a home computer through a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/), so you can use it from your phone.

1. In `.env`, set `WEB_PASSWORD` (4 characters or more) and optionally `WEB_PORT` (default 8795).
2. Run `cd electron && npm run server`. The site is on `http://127.0.0.1:8795` and asks for the password.
3. Point your tunnel or reverse proxy at that address. The site listens on this computer only, never directly on your network.

The site is off whenever `WEB_PASSWORD` is blank. Anyone with the password can build courses on your API keys, so pick a good one.

On Windows, `scripts\autostart.ps1` keeps the site running after a reboot and restarts it if it stops. Run it from an Administrator PowerShell to start the site at boot, or as yourself to start it when you sign in. `-Remove` takes it out again.

## Choosing models

Everything is in `config.yaml`: the model for each task, the fallback order, effort levels and how many videos to check. Every course folder gets a `build_report.json` with the models used, the videos checked and the approximate cost. To find the cheapest models that still write good courses for you:

```bash
python eval_models.py --estimate        # what a full evaluation would cost
python eval_models.py --topics 5        # a small trial
```

## Ready-made courses

Don't want to build your own? Browse finished courses at [teach-me-anything.com](http://teach-me-anything.com).

## Good to know

- **AI writes these courses, and AI makes mistakes.** Check anything important, and follow safety advice and local rules for electrical, gas, medical, legal or financial topics.
- **YouTube:** videos are found through the official YouTube Data API and play in YouTube's own player. Teach Me does not download videos or scrape transcripts.
- **Costs** depend on your models and your provider's prices. Each course's `build_report.json` shows what it cost.

## Tests

```bash
python -m unittest
cd electron && npm test
```

## License

[GNU AGPL-3.0](LICENSE). You may use, change and share Teach Me. If you run a changed version as a service for others, you must publish your changes under the same license.
