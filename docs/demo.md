# Demo page

A small Streamlit page for showing the system to someone: pick one of the nine AcmeCorp people, ask a question, and see the answer with its sources. A second tab asks one question as two people side by side, which is the point of the project in one screen: David in Sales and Grace, an executive, asking how executive bonuses are calculated.

AcmeCorp is fictional. Every document and person was made up for this project.

The page runs the application code in its own process rather than calling the API, so a visitor can switch person without signing in. What to show on it, step by step, with what will appear, is in the [demo scenario](demo-scenario.md); its examples are the pairs that scenario uses.

## What is real and what is demo-only

**Real:** everything from the question onwards. The page calls the same `answer_question` as the service does: the same access scope, second check, relevance gate, context, prompt, model call and citations. The answer tests in [generation](generation.md#the-boundary-tested-at-the-answer) cover exactly this path.

**Demo-only:**

- **Picking a person stands in for logging in.** In the service, who is asking comes from a signed token (decision record 0004). Here the visitor chooses, because comparing people is the point. A visitor can be anyone, so this page must only ever hold the fictional company.
- **Everything is in memory.** The company is loaded, embedded and indexed when the page starts, about 50 seconds on the development laptop. Nothing is saved, and a restart starts again.
- **Answers are kept per person and question,** so asking the same thing twice doesn't call the model again. The key includes the person, so one person's answer is never shown to another (T6). At most 500 are kept.
- **One question at a time.** The in-memory database is one SQLite connection, and Streamlit serves each visitor on its own thread.
- **A budget for the model, shared by every visitor:** 60 questions in any hour and 300 in any 24 hours, for everyone together. Visitors have no identity to count by, so the budget is one for the page. It lives with the demo, which is built once for the process, so reloading the page doesn't reset it; a restart does. A question that gets no information counts too, because whether the model is called depends on who is asking. A kept answer costs nothing and is still shown when the budget is spent. The numbers are a choice, not the provider's limits: they bound what anyone with the link can spend, whatever the provider would allow.
- **30 questions per visit,** so one visit can't use up the shared budget alone. Reloading starts a new visit, but not a new budget.

The budget protects the key's quota, not the demo: one person can still spend it and leave the page answering nothing for the hour. Sharing it fairly between visitors would need something to tell them apart, and the page has nothing it could trust.

With no model configured the page still works: it shows the passages each person would get, without answers.

## Running it locally

```
pip install -r requirements-dev.txt
streamlit run ui/streamlit_app.py
```

It reads `.env` like the rest of the project. With `APP_LLM_PROVIDER=groq` and a key, answers take about a second. With `ollama` they take about a minute; set `APP_LLM_TIMEOUT_SECONDS=300`.

## Deploying on Streamlit Community Cloud

1. Sign in at [share.streamlit.io](https://share.streamlit.io) with GitHub and create an app from this repository, branch `main`, main file `ui/streamlit_app.py`.
2. Under advanced settings, choose Python 3.11 and add the secrets:

   ```toml
   APP_LLM_PROVIDER = "groq"
   APP_LLM_API_KEY = "gsk_..."
   APP_LLM_MAX_OUTPUT_TOKENS = 1000
   ```

   The key stays in Streamlit's secrets; the page copies settings starting with `APP_` into the environment and never shows them. Leave out the first two lines to run without answers.
3. Deploy. Streamlit installs `ui/requirements.txt`, which takes PyTorch from its CPU-only index: the default Linux build brings several gigabytes of GPU libraries the demo doesn't use. The first start also downloads the embedding model.

With Groq, every question sends the person's passages, fictional company text, to Groq. For a real company that would be a decision for whoever owns the data ([generation](generation.md#providers)).

**Deployed** at [https://permission-aware-enterprise-knowledge-system-ij67laxbihrkmrqfw.streamlit.app/](https://permission-aware-enterprise-knowledge-system-ij67laxbihrkmrqfw.streamlit.app/), from this repository with the CPU-only PyTorch build, and checked by hand: David gets no information about executive bonuses, Grace gets the formula with its source. A free app sleeps when nobody has used it for a while, and the first visit after that waits for it to load the company again. Memory use and start time on Streamlit's servers have not been measured. Streamlit redeploys it on every push to `main`, without waiting for CI ([CI](ci.md#what-it-holds-back-and-what-it-doesnt)).

## Tests

- `tests/unit/test_demo.py`: the demo with the fake embedder and model. Everyone sees only what they may read, kept answers never cross between people, the oldest kept answer goes first, no model means passages only, nine visitors at once, and the budget: shared by every visitor, refused before the model is asked, kept answers still given.
- `tests/integration/test_demo_budget.py`: the page itself, headless, with the fake embedder and model and a budget of two. A second visit, as a reload is, finds the budget spent by the first.
- `tests/integration/test_demo_page.py` (`pytest -m model`): the page itself, run headless with the real embedding model and no language model.
- `tests/unit/test_demo_requirements.py`: what Streamlit Cloud installs matches what is installed and tested here.
