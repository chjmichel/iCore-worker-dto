# iCore DTO worker for Operaton

The worker subscribes to Operaton External Tasks, reads the deployed BPMN mapping, resolves the operation against the iCore OpenAPI 3.0 YAML and calls iCore using a separate worker identity. It returns the JSON response, HTTP status and mapping trace to Operaton. A single worker handles the catalog's JSON DTO operations.

## Run with Docker Compose

1. Copy `.env.example` to `.env`.
2. Set `ICORE_CATALOG_FILE` to the path of your trusted iCore Swagger YAML. The default is `./catalog.yaml`. Use the same YAML revision in the Orchestrator and the worker.
3. Set `OPERATON_ENGINE_URL` to the REST base URL, including `/engine-rest`, and supply its basic-auth credentials if required. Use a dedicated account with access to read deployed process definitions and work the relevant external tasks.
4. Set `ICORE_BASE_URL` to your iCore HTTPS origin. Set `ICORE_USERNAME` and `ICORE_PASSWORD` for managed worker login, or set `ICORE_BEARER_TOKEN`. Managed login uses `/api/v2/icore/login`, caches the token in memory and renews it near expiry. `ICORE_LOGIN_FORMAT=json` is available if the tenant expects a JSON login payload. `ICORE_LOGIN_ORIGIN` supplies the optional Origin and Referer. For mounted secrets, set `ICORE_PASSWORD_FILE` to the in-container path and mount the file read-only in a Compose override.
5. Run `docker compose up --build -d` and check `docker compose logs -f worker`.

The default topic, `icore-service`, matches iCore Process Orchestrator. Use one worker implementation per topic per engine. To run this worker alongside the Orchestrator worker, set `WORKER_TOPIC=icore-service-dto` and use the same topic in the BPMN models assigned to it. Keep the worker at one replica with the persistent SQLite volume. The Operaton distribution needs a JSON variable serializer such as Spin.

## Generate a BPMN model from Swagger

With Python 3.12 and the catalog available locally:

```sh
python -m pip install -r requirements.txt
python generate.py --catalog catalog.yaml list
python generate.py --catalog catalog.yaml bpmn detail_33 --output product-read.bpmn
```

With the iCore fixed YAML, `detail_33` is the read-only product lookup. Deploy the generated BPMN to Operaton. Start process key `icore_dto_example` with an existing numeric product ID, for example this body to `POST /process-definition/key/icore_dto_example/start` under the engine REST base URL:

```json
{
  "variables": {
    "id": { "type": "Long", "value": 1 }
  },
  "withVariablesInReturn": false
}
```

After completion, read `icore_result` from process history or consume it in a subsequent BPMN step. For a different operation, use its `operationId` from `list`. The generator prints the required input mappings. A required JSON body is supplied as a process variable named `requestBody`. Use `--process-key` to give each generated model a distinct key, and `--topic` when using a custom topic.

## Orchestrator mapping contract

The worker reads `icore-config` under the BPMN service task's Camunda or Operaton extension properties. For an API task it checks the method, path and operation ID against the YAML. The catalog hash is SHA-256 over UTF-8 with LF newlines and no BOM. A different catalog hash creates an incident before the iCore request.

- Mapping versions 1 and 2 support ordinary API tasks with constants, process variable paths, fallbacks, basic conversions, named outputs, process input defaults and the Orchestrator's process result collection task.
- API inputs can target `path.<name>`, `query.<name>`, `header.<name>`, `body` and `body.<field>`. The worker checks declared parameters, required fields and basic JSON types against the YAML. It passes through successful JSON responses and accepts empty successful responses.
- The named result is an Operaton `Json` variable with `response`, `status` and `trace`. Process history and the pending-result database may contain business payloads and mapped inputs. Apply the same access and retention controls to both.
- The worker supports the default iCore connection. Run the Orchestrator's own worker for its script, transform and expression tasks or models requiring multiple named connections. The supported HTTP contract covers JSON APIs. The catalog listing identifies unsupported request encodings, including multipart uploads, and reserves login operations for worker authentication.

## Failure and replay behavior

Transient read failures use Operaton retries. Write methods run once per attempt and carry the external task ID as the `Idempotency-Key`. The worker stores a dispatch marker before the iCore call and persists the result before acknowledging completion. If completion fails, it reuses the stored result on the next lock. An uncertain write raises an incident. Reconcile it in iCore before deciding whether to resume from the existing business result or perform a new write. Keep the state volume when restarting or updating the worker.

## Tests

```sh
python -m unittest discover -s tests -v
TEST_ICORE_CATALOG=./catalog.yaml python -m unittest discover -s tests -v
```

The tests use loopback HTTP doubles for Operaton and iCore. Setting `TEST_ICORE_CATALOG` to the fixed iCore YAML also exercises the real product operation definition.
