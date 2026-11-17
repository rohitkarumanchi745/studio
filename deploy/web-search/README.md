# Web search with Grounding with Bing

Studio's agents can answer anything, including questions that need the web.
Search goes through **Grounding with Bing Search** in an Azure AI Foundry
project. The standalone Bing Search APIs were retired in 2025, and this is the
supported way to ground AI answers in Bing. Studio reads pages itself with its
own page reader.

Until this is set up, Claude models use Anthropic's web search
(`STUDIO_WEB_SEARCH=auto`). Every other model can still read a page it is
given.

## Set up (Azure)

1. **Create a Foundry project** and deploy a model in it, for example
   `gpt-4.1-mini`. That model writes the grounded summary that Studio's
   agent reads.
2. **Create a Grounding with Bing Search resource**, then connect it to the
   project (Project → Connected resources). Copy the connection's ID. It has
   this form:
   `/subscriptions/…/resourceGroups/…/providers/Microsoft.CognitiveServices/accounts/…/projects/…/connections/…`
3. **Create a service principal** (an app registration with a client
   secret). Give it the **Foundry User** role on the project.
4. **Set these on the server** (Railway variables, or `backend/.env`
   locally):

   | Variable | Value |
   |---|---|
   | `FOUNDRY_PROJECT_ENDPOINT` | `https://<resource>.ai.azure.com/api/projects/<project>` |
   | `FOUNDRY_MODEL_DEPLOYMENT_NAME` | the model deployment from step 1 |
   | `BING_PROJECT_CONNECTION_ID` | the connection ID from step 2 |
   | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | the service principal from step 3 |
   | `BING_MARKET` (optional) | default `en-US` |

Each search costs one Grounding with Bing transaction plus the deployed
model's tokens.

## What Studio does to meet the Grounding with Bing terms

- **References are shown.** Every Bing-grounded answer shows the Bing
  results link for each query and the website links it used, next to the
  answer. It is marked "From a Bing web search".
- **No training.** Answers grounded in Bing, and later turns whose history
  holds one, are kept out of BitNet training, Agent Lightning, learned rules
  and the plan cache. Their text is not kept in training traces.
- **No result store.** Search output is never cached or indexed. It lives
  only in the answer it grounded.
- **robots.txt.** The page reader honours it, and identifies itself as
  `StudioBot`.

You remain responsible for deciding whether Grounding with Bing fits your
compliance needs. Queries leave the Azure compliance boundary for Bing (see
Microsoft's
[Grounding with Bing terms](https://www.microsoft.com/bing/apis/grounding-legal-enterprise)).

## The page reader

`web_fetch` reads one public http(s) page as text. It connects only to an
address it has checked is public: never loopback, private, link-local or
cloud metadata. It checks every redirect again, verifies TLS for the site's
name, reads at most 2 MB, and handles only text, HTML and JSON.

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
