# Shipyard CI: the API (a test system)

Shipyard CI is an invented deployment system. MaximizePM does not ship a
plugin for it. It exists to check that the guide `maxpm guide pipelines` is
enough to write a plugin (#6165). Its recorded answers are in `answers/`.

Each Shipyard project has jobs (`build`, `deploy`, ...). One run of a job is
a **deploy**. Every request carries the header `X-Shipyard-Key: <API key>`.
The base address is the setting of the account, for example
`https://ci.example.com`.

## Read the deploys that changed

`GET /api/projects/<project>/deploys?after=<cursor>`

Each change of a deploy gives it a new, higher `cursor`. The answer lists the
deploys that changed after the cursor you send (send 0 the first time), each
one as it is now, and the newest cursor:

```json
{"cursor": 7,
 "deploys": [{"id": 101, "cursor": 5, "job": "build", "commit": "3f2a9c1...",
              "state": "ok", "started": "2026-11-19T09:00:00Z", "ended": "2026-11-19T09:04:00Z",
              "link": "https://ci.example.com/p/shop/deploys/101", "tag": "",
              "tasks": [{"name": "compile", "state": "ok", "started": "...", "ended": "..."}]}]}
```

- `state`: `waiting`, `running`, `ok`, `failed` or `stopped`.
- `tag`: the free text that the request that started the deploy gave (empty
  for a deploy that a push started).
- `ended` is null while the deploy runs.

## The test results of a deploy

`GET /api/projects/<project>/deploys/<id>/results`: the JUnit XML of the
deploy, as text (`Content-Type: application/xml`). 404 when the deploy wrote
no results.

## Start a deploy

`POST /api/projects/<project>/deploys` with `{"job": "deploy", "commit":
"<commit>", "tag": "<free text>"}`: the answer is `{"id": <new deploy id>}`.

## Stop, and run again

- `POST /api/projects/<project>/deploys/<id>/stop`: `{}`.
- `POST /api/projects/<project>/deploys/<id>/again`: a new deploy of the same
  job and commit, `{"id": <new id>}`.

Shipyard has no roll back: a person starts a deploy of an older commit.

## Errors

A status of 400 or more, with `{"problem": "<words>"}`. 401: a wrong key.

## The recorded answers

`answers/` holds answers of a Shipyard project `shop`:

- `deploys-after-0.json`: `GET .../deploys?after=0` (build 101 passed, build
  102 runs).
- `deploys-after-7.json`: `GET .../deploys?after=7` before anything new.
- `results-101.xml`: `GET .../deploys/101/results` (other ids: 404).
- `start-deploy.json`: `POST .../deploys`.
- `deploys-after-7-started.json`: `GET .../deploys?after=7` after that start:
  deploy 103 passed. Its `tag` is the tag the start sent (here a placeholder).
- `stop.json`: `POST .../deploys/<id>/stop`.
- `error-401.json`: any request with a wrong key (status 401).
