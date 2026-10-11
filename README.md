# ResilienceAI: AI-driven chaos engineering for cloud-native resilience

**Hackathon track:** AI-Driven Chaos Engineering for Cloud-Native Resilience (BGSW / Bosch)

ResilienceAI is a chaos-engineering control center. Three AI agents, each running on a different local
language model, deliberately break parts of a real microservices application inside a safe sandbox. A
safety controller validates and limits every fault, live telemetry measures exactly what shoppers
experience, and a separate analyst model ranks the damage by severity and recommends fixes. One click
rolls everything back and proves the system recovered.

---

## 1. The problem

Modern applications are built from many small services that call each other. When one of them fails or
slows down, the failure can spread in ways nobody predicted: a broken advert service should not stop
people from buying, but in many systems it does. Teams usually discover these weak points during a real
outage, when it is most expensive.

**Chaos engineering** finds them earlier by breaking things on purpose, in a controlled way, and
measuring what happens. Doing it by hand is slow, and humans tend to test the same few scenarios. Our
question was: *can AI agents choose varied, meaningful failures, run them safely, and explain the
results?*

## 2. What we built

| Capability | What it does |
|---|---|
| Real target system | Google's **Online Boutique**: an 11-service online shop (Go, C#, Node.js, Python, Java, gRPC) running on Kubernetes |
| Dependency map | Discovered automatically from the Kubernetes manifests: which service calls which |
| Baseline | 60 s of healthy traffic measured first: error rate 0%, p95 latency about 26 ms |
| 3 fault types | **Outage** (service scaled to 0), **crash loop** (pods killed repeatedly), **network latency** (fixed delay on every call into a service) |
| 3 AI agents | Three different local models each choose and inject one error; roles and targets are randomised every run |
| Live telemetry | Synthetic shoppers browse, add to cart and check out continuously; every request is measured in 3-second windows |
| Error attribution | Each failed, slow or degraded request is traced to the service that caused it |
| Severity ranking | A separate analyst model ranks the three errors (Critical / High / Medium / Low) from measured evidence |
| Fix recommendations | For each error: likely cause, immediate fix, long-term fix, prevention |
| Resilience score | One 0-100 number for the run |
| Kill switch | Freezes the system instantly, exactly as it is |
| Rollback | Restores the initial state and verifies recovery with live telemetry |
| Findings report | Exported as Markdown from the Findings page |
| Resilient frontend | We patched the shop so one broken service only breaks the pages that need it |

## 3. How it works

```
 ┌───────────────────────┐   pick (JSON)   ┌──────────────────────────┐   kubectl / tc   ┌────────────────────────────┐
 │ 3 AI agents (Ollama)  │ ──────────────▶ │ Safety controller        │ ───────────────▶ │ Kubernetes sandbox (kind)  │
 │  A qwen3:4b           │                 │  allowlist + schema      │                  │  Online Boutique, 11 svcs  │
 │  B ministral-3:3b     │                 │  healthy-before check    │                  │  namespace "boutique"      │
 │  C llama3.2:3b        │                 │  kill switch, 10-min cap │                  └─────────────┬──────────────┘
 └───────────────────────┘                 └──────────────────────────┘                                │
             ▲                                                                                         │ synthetic shoppers
             │ dependency map, roles,                                                                  ▼
             │ random shortlist              ┌──────────────────────────┐   evidence   ┌────────────────────────────┐
 ┌───────────┴───────────┐                   │ Analyst model gemma3:4b  │ ◀─────────── │ Telemetry (3 s windows)    │
 │ FastAPI backend       │ ◀──────────────── │  severity ranking        │              │  error rate, p95 latency,  │
 │ React control center  │   findings        │  fix recommendations     │              │  failed / slow / degraded, │
 └───────────────────────┘                   └──────────────────────────┘              │  attribution per service   │
                                                                                        └────────────────────────────┘
```

### One run, step by step

1. **Launch.** The operator presses *Launch 3 agents*. Telemetry starts automatically if it is off.
2. **Roles are shuffled.** The three error types are randomly assigned to the three agents.
3. **Each agent chooses.** It receives the dependency map, the user journeys and a random shortlist of 4
   services (avoiding the previous run's targets). Its model must answer in a fixed JSON format naming
   one service from that shortlist and, for latency, a delay. The agents take turns because the laptop GPU
   fits one model at a time; each takes 5-70 s.
4. **Safety gate.** Every pick is validated (allowed service, allowed fault, limits). Two agents may not
   pick the same service, or a pair the shop cannot tell apart (cartservice and redis-cart). The shop must
   show healthy telemetry before anything is injected.
5. **Inject and hold.** All three errors are injected together and held until rollback or the kill switch
   (automatic removal after 10 minutes as a backstop).
6. **Observe.** For 30 s, telemetry records every shopper request and attributes each failure to a service.
7. **Rank.** The analyst model ranks the three errors by severity using the measurements.
8. **Recommend.** The analyst writes a fix for each error, in rank order.
9. **Roll back.** Every change is reverted, and recovery is verified from live telemetry.

## 4. The AI part

### Models

| Role | Model | Maker | Runs on |
|---|---|---|---|
| Agent A | qwen3:4b (with a visible reasoning phase) | Alibaba | Laptop GPU via Ollama |
| Agent B | ministral-3:3b | Mistral AI | Laptop GPU via Ollama |
| Agent C | llama3.2:3b | Meta | Laptop GPU via Ollama |
| Analyst | gemma3:4b (optional: gemma4:31b-cloud) | Google | Laptop GPU via Ollama (cloud option needs `ollama signin`) |

Four models from four different makers. All run locally: no API key, no data leaves the laptop, no
cost per call.

**We did not train these models.** They are pre-trained, open-weight models. Our work is how they are
used: the prompts, the context they receive, the output formats they are forced into, and the
validation and measurement around them.

### How the agents are kept safe and useful

- **Structured output.** Ollama constrains each model's output to a JSON schema. For the agents, the
  `target` field is an enum of exactly the shortlisted services, so the model *cannot* name anything else.
- **Models only return data, never commands.** A pick is a small JSON object. The fault itself is executed
  by fixed, reviewed code templates.
- **Deterministic validation.** Every pick passes `controller.validate()`: allowlisted target, known fault
  type, parameters within limits.
- **Retry and fallback.** If a model's answer is invalid twice, a random valid pick is used and clearly
  flagged as "random fallback" in the UI.
- **Variety by design.** Shuffled roles, random shortlists, random sampling seed and temperature 0.9, so
  runs differ. In two consecutive test runs no service was chosen twice.

### Severity ranking (separate function: `chaos/severity.py`)

1. **Measure** each error's impact from telemetry: failed, slow and degraded requests attributed to that
   service, which pages broke, and which user journeys failed.
2. **Formula score (0-100)**, transparent and reproducible:

   | Factor | Weight | Measured as |
   |---|---|---|
   | User impact | 40% | Share of all shopper requests that failed because of this error |
   | Business impact | 25% | Worst broken journey: checkout 1.0, cart 0.7, browse 0.6 |
   | Blast radius | 20% | Pages broken out of 5 (home, product, view cart, add to cart, checkout) |
   | Slowdown | 15% | Share of requests that were slow |

   Labels: 70+ Critical, 40-69 High, 20-39 Medium, under 20 Low.
3. **Analyst ranking.** The analyst model receives the measurements and formula scores, ranks the errors
   and explains why. Its answer must be a valid ranking of exactly these errors; otherwise the formula
   ranking is used and the UI says so.

**Resilience score** = 100 - (0.6 x worst score + 0.4 x average score). One critical error dominates the
score, as it would for real users.

### Fix recommendations (`chaos/fixes.py`)

For each error, in severity order, the analyst receives the fault type, the service, the measured impact
and sample error messages, and must return: likely cause, immediate fix, long-term fix, prevention and a
confidence level. Recommendations are advice for people and are never executed.

## 5. Safety

| Safeguard | How |
|---|---|
| Sandbox only | Only the `kind-chaos-sandbox` cluster context and the `boutique` namespace |
| Allowlist | Explicit list of services that may be targeted; the shop's entry point is excluded from the agents |
| Healthy before injecting | Injection is refused unless live telemetry shows a healthy shop (waits up to 45 s to settle) |
| Validation | Every AI pick is checked by deterministic code before anything changes |
| Kill switch | Stops all agents and freezes the system exactly as it is (crash loops pinned at 0 replicas) |
| Rollback | Restores replica counts, removes network delays, ends crash loops, then verifies recovery |
| Emergency guardrail | Automatic rollback if 90% of shopper requests (or 95% of browsing) fail continuously for 30 s; set high on purpose so normal damage is held for observation and manual rollback |
| Timed experiments | Command-line experiments abort automatically at 10% errors or 2x baseline latency for 6 s |
| Backstop | Any held fault is removed automatically after 10 minutes |
| Audit trail | Every event, run, squad run and error is recorded and shown in History |

## 6. Resilience fix to the shop itself

Testing showed that Google's original frontend crashed the **whole site** when almost any single service
failed: the cart counter or the currency list appear on every page, and calls had no time limit. We
patched the Go frontend (`microservices-demo/src/frontend`):

- **Graceful degradation:** a page shows a yellow notice and a fallback (prices in USD, cart badge hidden,
  "shipping unavailable") instead of an error page.
- **Per-call timeouts:** 1.5 s default, 0.8 s for currency, 0.5 s for optional features.
- **Faster reconnects** after an outage (gRPC backoff capped at 2 s).
- An `X-Degraded` header tells our telemetry which service a page had to do without.

Switch between versions: `python patches\use_frontend.py original` or `... resilient`.

## 7. Results

### Isolation: one service down at a time (patched frontend)

| Service taken down | Pages that fail | Pages that keep working |
|---|---|---|
| redis-cart (cart database) | view cart, add to cart, checkout | home and product (cart badge hidden) |
| currencyservice | checkout | all others (prices shown in USD) |
| shippingservice | checkout | all others ("shipping unavailable" on the cart page) |
| productcatalogservice | product, add to cart, checkout | home (with notice), view cart |
| checkoutservice | checkout | all others |
| paymentservice | checkout | all others |
| recommendationservice | none | all (recommendations section hidden) |
| adservice | none | all (advert hidden) |
| emailservice | none | all (order still placed) |

Before the patch, several of these outages broke every page of the shop. Recovery after rollback was
verified every time, in 9-33 s.

### Example 3-agent run

| Rank | Severity | Service | Fault (agent / model) | Measured impact |
|---|---|---|---|---|
| 1 | High | productcatalogservice | Outage (A / qwen3:4b) | 102 of 187 requests failed (54.5%); product and add-to-cart pages broken |
| 2 | Medium | cartservice | Crash loop (B / ministral-3:3b) | 0 failed, 61 degraded: the shop kept working with fallbacks |
| 3 | Low | checkoutservice | +2500 ms latency (C / llama3.2:3b) | No measurable impact |

Resilience score: **46 / 100 (Fragile)**. Rollback: recovery verified in 15 s.

**Insight:** the checkout slowdown showed no impact because the catalog outage stopped shoppers at
"add to cart", so they never reached checkout. One failure masked another. This is exactly the kind of
interaction chaos engineering is meant to reveal.

## 8. The control center (React frontend)

| Page | What it shows |
|---|---|
| Every page | Live service dependency map (healthy / chosen / fault injected / affected, live error counts per service), backend connection, Kill switch, Roll back |
| Overview | KPIs vs baseline, current experiment, telemetry start/stop, random injection, the 3-agent squad, severity ranking and fix recommendations, manual experiment, latency and error-rate charts with fault windows shaded, affected services |
| Experiments | The same controls, focused on running experiments |
| Findings | Resilience score, ranked findings with measured impact, suspected cause and recommended fix; Markdown report export |
| History | Events, experiments, squad runs and errors as sortable, filterable tables |

Findings are shown only for the run in progress; after a rollback they clear and the run moves to History.
Only data returned by the backend is displayed; missing data is shown as "No data", never as placeholders.

## 9. Technology stack

| Layer | Technology | Used for |
|---|---|---|
| Target app | Google Online Boutique (Go, C#, Node.js, Python, Java, gRPC) | The system under test |
| Infrastructure | Docker Desktop (WSL 2), **kind**, Kubernetes, kubectl, Linux `tc netem` | Sandbox cluster and fault injection |
| Backend | Python, **FastAPI**, Uvicorn, **Pydantic**, Requests, PyYAML | API, safety controller, telemetry, agents, analysis |
| AI runtime | **Ollama** with JSON-schema structured output | Running the 4 models locally |
| Frontend | **React 18**, TypeScript, **Vite**, **Tailwind CSS 4**, Recharts, React Flow, lucide-react | Control center |
| Fallback UIs | Plain HTML/JS dashboard; Streamlit (`dashboard/app.py`) | Backup dashboards |

No agent framework (LangChain, CrewAI, AutoGen) is used: the agents are a few hundred lines of our own Python,
which keeps every step visible and auditable.

## 10. Running it (Windows, PowerShell)

### Prerequisites

- Docker Desktop (WSL 2 backend), `kind`, `kubectl`
- Python 3.11+ with a virtual environment in `.venv`: `pip install -r requirements.txt`
- Node.js 20+
- Ollama with the models:

```powershell
ollama pull qwen3:4b
ollama pull ministral-3:3b
ollama pull llama3.2:3b
ollama pull gemma3:4b
```

Optional larger cloud analyst: `ollama signin`, then `$env:CHAOS_ANALYST_MODEL = "gemma4:31b-cloud"`
before starting the backend.

### First time: create the cluster and deploy the shop (about 10 minutes)

```powershell
.\setup-cluster.ps1
.\chaos.cmd baseline        # capture the healthy baseline (60 s)
```

### Every time: start everything

```powershell
.\start-dashboard.ps1
```

This opens a window each for the shop, the backend and the frontend, starts telemetry, and opens the
control center.

| What | Address |
|---|---|
| ResilienceAI control center | <http://localhost:5173> |
| The shop | <http://localhost:8080> |
| Backend API (and built-in dashboard) | <http://127.0.0.1:8000> (docs at `/docs`) |

Frontend on its own: `cd frontend; npm install; npm run dev`. To point it at another backend, copy
`frontend\.env.example` to `frontend\.env` and set `VITE_API_URL`.

### Command line

```powershell
.\chaos.cmd random          # inject one random fault
.\chaos.cmd stop            # kill switch
.\chaos.cmd rollback        # restore the initial state
```

## 11. Backend API

| Method | Path | Purpose |
|---|---|---|
| GET | `/status` | State, active faults, current job, baseline |
| GET | `/metrics` | 3-second telemetry windows |
| GET | `/errors` | Failed / slow / degraded requests by service |
| GET | `/services` | Services with health and dependencies (the map) |
| GET | `/events`, `/experiments/history`, `/squad/history` | History |
| POST | `/ingestion/start`, `/ingestion/stop` | Synthetic shopper traffic |
| POST | `/squad/launch` | Three agents choose and inject one error each |
| GET | `/squad`, `/squad/severity`, `/squad/fixes` | Live squad state, ranking, fixes |
| POST | `/squad/analyze` | Re-run ranking and fixes on the latest telemetry |
| GET | `/findings` | Resilience score and ranked findings for the active run |
| POST | `/experiments`, `/experiments/random-errors` | Manual or random single fault |
| POST | `/experiment/stop` | Kill switch |
| POST | `/rollback` | Restore and verify (`{"confirm": true}`) |

## 12. Project structure

```
chaos/
  api.py           FastAPI backend: endpoints, job scheduling, rollback
  squad.py         the 3 agents: random roles, shortlists, picks, hold loop
  severity.py      impact measurement, formula score, analyst ranking
  fixes.py         fix recommendations
  analyst.py       analyst model selection and fallback
  llm.py           structured-output calls to Ollama
  controller.py    validation, single-experiment runner, kill switch
  faults.py        fault templates: outage, crash loop, network delay; rollback
  telemetry.py     synthetic-shopper ingestion, 3 s windows, error attribution
  probe.py         the synthetic shopper
  discovery.py     dependency map from Kubernetes manifests
  safety.yaml      safety limits
frontend/          React control center
dashboard/app.py   Streamlit dashboard (fallback)
patches/           frontend image switch and template patch
microservices-demo/  Online Boutique, with the patched Go frontend
out/               baseline, runs, squad runs
```

## 13. Limitations and next steps

- **Small models:** 3-4 B parameter models occasionally give weak reasons; the schema and validation
  keep their *actions* safe, but the explanations can be generic. A larger cloud analyst is supported.
- **Laptop scale:** one model runs at a time, so choosing takes about 1-2 minutes per run.
- **Attribution limits:** when several faults hit the same page, the error is credited to the most
  likely service; masking (one fault hiding another) is reported rather than corrected.
- **Next:** more fault types (CPU and memory pressure, packet loss), multi-replica targets to test
  failover, learning from past runs to steer the agents toward untested weak spots, and automatic
  verification that a recommended fix actually improves the score.

## 14. Hackathon deliverables

| Deliverable | Where |
|---|---|
| Source code and README | This repository |
| Architecture overview | Sections 3-5 of this README; live dependency map in the control center |
| AI findings report | Findings page, *Export report* (Markdown) |
| Demo video (3 min or less) | Launch 3 agents, live map turns red, ranking and fixes appear, kill switch, rollback with verified recovery |
