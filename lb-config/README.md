# lb-config

Convert a **LoopBack 3** server configuration into a **NestJS 12
`@nestjs/config`** setup.

A LoopBack app configures itself with declarative JSON under `server/`:
`config.json` (app host/port/restApiRoot/remoting), `datasources.json`
(one object per named datasource), and `component-config.json` (components
mounted at boot). Each is layered per environment by `.local.{json,js}` and
`.<NODE_ENV>.{json,js}` files. Because it is data, the bulk of the port to
NestJS is a deterministic transform — this script does it.

`lb_config.py` reads those files, merges the environment layering the way
`loopback-boot` does, and generates a typed `ConfigModule` scaffold plus a
report of what maps where and what still needs a human.

Python 3.9+, no dependencies. Emits TypeScript; nothing here runs Node.

## Usage

```bash
# Print the migration report to stdout (default)
./lb_config.py path/to/loopback-app

# Choose the NODE_ENV layer to merge (default: $NODE_ENV or development)
./lb_config.py path/to/loopback-app --env production

# Write the generated files into a directory
./lb_config.py path/to/loopback-app -o ./nest-config

# Preview without writing / overwrite a non-empty dir
./lb_config.py path/to/loopback-app -o ./nest-config --dry-run
./lb_config.py path/to/loopback-app -o ./nest-config --force
```

`path/to/loopback-app` may be the app root (config found under `server/`) or
the `server/` directory itself.

## What it generates

| File | Contents |
|------|----------|
| `config/app.config.ts` | `registerAs('app', ...)` — `host`, `port`, and `globalPrefix` (from `restApiRoot`), read from env vars. |
| `config/database.config.ts` | One `registerAs` namespace per datasource (`database`, then `database_<name>`); connection fields read from env vars. |
| `config/configuration.ts` | A default factory aggregating the namespaced factories: `ConfigModule.forRoot({ load: [configuration] })`. |
| `.env.example` | Every env var the config references, with placeholders — secret values are `change-me`, never the real input value. |
| `CONFIG_MIGRATION.md` | Mapping tables (app keys, datasource fields, components) and a manual-attention checklist. |

## How the environment layering is handled

LoopBack merges configuration in ascending precedence
(`X.json` < `X.local.json` < `X.<env>.json`, later wins). The script performs
that JSON merge for `config`, `datasources`, and `component-config`, selecting
the `<env>` layer from `--env` / `$NODE_ENV`.

The `.local.js` / `.<env>.js` variants **execute code** at boot, so they are
**never evaluated** — they are detected and flagged in the report for a human
to port by hand.

## Secrets

Any field whose name looks sensitive (`password`, `secret`, `token`,
`apiKey`, plus connection `url`s that can embed credentials) is emitted as a
bare `process.env.X` reference with no literal fallback, and listed in
`.env.example` with a placeholder. A real secret present in the input is
**never** copied into generated code.

## Wiring the result

```ts
import { ConfigModule } from '@nestjs/config';
import appConfig from './config/app.config';
import databaseConfig from './config/database.config';

@Module({
  imports: [
    ConfigModule.forRoot({
      isGlobal: true,
      load: [appConfig, databaseConfig],
      envFilePath: '.env',
    }),
  ],
})
export class AppModule {}
```

`restApiRoot` maps to a global prefix — apply it in `main.ts`:

```ts
const cfg = app.get(ConfigService);
app.setGlobalPrefix(cfg.get<string>('app.globalPrefix'));
await app.listen(cfg.get<number>('app.port'), cfg.get<string>('app.host'));
```

Read values with `configService.get('app.port')` /
`configService.get('database.host')`, or inject a namespace typed via
`ConfigType<typeof databaseConfig>`.

## Tests

```bash
python3 lb-config/test_lb_config.py
```

Zero third-party deps. The fixture builds a temp `server/` with a base
`config.json`, a `config.production.json` override, an executable
`config.local.js`, and a `datasources.json` carrying a password, then asserts
that the env override wins, the `.js` variant is flagged (not evaluated), the
password becomes a `process.env` reference (never the literal), and
`.env.example` lists it.
