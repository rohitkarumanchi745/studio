# Web search: Bing, Google, Brave, or Claude's own

Studio's agents can answer anything, including questions that need the web.
Every chat has a **web toggle** next to the model picker. It can be **Off**
(no web at all), **Auto**, or one provider the server has configured.
`STUDIO_WEB_SEARCH` on the server sets the limits: `off`, `auto` (any
configured provider), or a single provider.

| Provider | Setup | How its results are used |
|---|---|---|
| **Bing** (Grounding with Bing Search) | Azure AI Foundry, below | Bing's grounded answer **is** the answer, exactly as it comes back, with the Bing results link and the page links |
| **Google** (Grounding with Google Search) | `GEMINI_API_KEY` | Google's grounded answer **is** the answer, exactly as it comes back, with Google's Search Suggestions and the page links. Shown **only to the person who asked**, and removed after **two years** |
| **Brave** (Brave Search API) | `BRAVE_SEARCH_API_KEY` on a *Data for AI* plan | Plain results that the agent answers from, citing them |
| **Claude web search** (Anthropic) | an Anthropic key | Claude searches and answers with citations (Claude models only) |

With **Auto**, Studio uses the first of these that is configured. The person
can pick another in the toggle. Every model can also read a public page it is
given, using Studio's own page reader.

Why Bing and Google answers aren't reworded: Bing's terms say "Customer will
not modify or create a derivative work based on any Bing Services Output".
Google's say grounded results may not be modified, "interspersed" with other
content, analyzed or learned from. So when an agent searches with them, the
search ends its turn and their answer is shown as it came back. It is never
mixed with data from a query or fed to a model in later turns. Answers like
this can't be sent to a Teams or Slack channel.

No provider's output trains anything: BitNet, Agent Lightning, learned rules
and the plan cache all skip it.

## Bing: Grounding with Bing Search (Azure AI Foundry)

The standalone Bing Search APIs were retired in 2025. This is the supported way
to ground AI answers in Bing.

1. **Create a Foundry project** and deploy a model in it, for example
   `gpt-4.1-mini`. It writes Bing's grounded answer.
2. **Create a Grounding with Bing Search resource** and connect it to the
   project (Project → Connected resources). Copy the connection's ID
   (`/subscriptions/…/connections/…`).
3. **Create a service principal** (an app registration with a client
   secret). Give it the **Foundry User** role on the project.
4. **Set these on the server:** `FOUNDRY_PROJECT_ENDPOINT`
   (`https://<resource>.ai.azure.com/api/projects/<project>`),
   `FOUNDRY_MODEL_DEPLOYMENT_NAME`, `BING_PROJECT_CONNECTION_ID`,
   `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, and optionally
   `BING_MARKET` (default `en-US`).

Terms: [Grounding with Bing](https://www.microsoft.com/bing/apis/grounding-legal-enterprise).
Queries leave the Azure compliance boundary for Bing.

## Google: Grounding with Google Search (Gemini API)

Google's Custom Search JSON API is closed to new customers and ends on
1 January 2027, so Studio reaches Google through Gemini's grounding.

1. Create a Gemini API key in Google AI Studio.
2. Set `GEMINI_API_KEY`. Optionally set `GOOGLE_GROUNDING_MODEL` (default
   `gemini-2.5-flash`); it must be a model that supports Grounding with
   Google Search.

What Studio does to follow the
[Gemini API terms](https://ai.google.dev/gemini-api/terms):

- **Only the person who asked sees it.** Everyone else in a shared chat sees
  a note instead.
- **Two years at most.** It is hidden after two years and then removed. The
  retention ticker does this whenever Google is configured.
- **Search Suggestions are shown with it**, as Google renders them, in a
  frame that runs no script and opens links in a new tab.
- **No click tracking** is added to its links, and no content is placed
  between them and their pages.

Billing: Gemini 3 and newer models bill each search query; older models bill
per prompt (see Google's pricing).

## Brave Search API

Set `BRAVE_SEARCH_API_KEY`. Use a **Data for AI** plan: those plans grant the
right to use results in AI answers. Storing results beyond the answer they
ground needs a plan with storage rights. Optionally set `BRAVE_COUNT`
(default 8) and `BRAVE_COUNTRY`.

## The page reader

`web_fetch` reads one public http(s) page as text. It connects only to an
address it has checked is public: never loopback, private, link-local or
cloud metadata. It checks every redirect again, verifies TLS for the site's
name, honours robots.txt (it identifies as `StudioBot`), reads at most 2 MB,
and handles only text, HTML and JSON.

## Files on this machine

Set `STUDIO_LOCAL_FILES_DIR` to one folder. Agents working for admins and
analysts can then list, search and read files in it (text, Markdown, CSV,
JSON, code, PDF, Excel, Word). They never write to it.

- Hidden files (`.env`, `.git`) and anything outside the folder are never
  read. Links that point outside it are skipped too.
- A file iCloud has moved off the Mac is reported instead of read, because
  reading it would wait for a download. Pick a folder that is not
  iCloud-synced.
- Leave it unset on cloud deployments.
