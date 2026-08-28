# lb-filter

`lb_filter.py` — a **code generator** (Python 3.9+, no dependencies) that emits
a reusable **NestJS 12 + TypeORM** TypeScript module implementing a
`LoopbackFilterPipe`. The pipe lets a NestJS app keep answering the LoopBack 3
`filter` query language, so existing API clients don't break after a
LoopBack → NestJS migration.

LoopBack 3 exposes a rich REST query language through one `filter` query string:

```
GET /coffeeShops?filter={"where":{"city":"NYC","rating":{"gte":4}},
                         "include":"reviews","order":"name ASC",
                         "limit":10,"skip":20,"fields":["id","name"]}
```

The generated code parses that `filter` (a JSON string **or** an already-parsed
object) and converts it into a TypeORM
[`FindManyOptions`](https://typeorm.io/find-options)
(`where` / `relations` / `order` / `take` / `skip` / `select`). The translation
logic lives entirely in the emitted TypeScript — this Python script only writes
the `.ts` files (the repo is Python/no-deps and emits TypeScript, matching
`loopback-to-nest/lb2nest.py`).

## Usage

```bash
./lb_filter.py                    # write the module into ./loopback-filter
./lb_filter.py -o src/common/lb   # choose the output directory
./lb_filter.py --dry-run          # list files, write nothing
./lb_filter.py --force            # overwrite a non-empty output directory
./lb_filter.py --stdout           # print all files to stdout, write nothing
```

Exit codes: `0` ok, `1` refusing to overwrite a non-empty directory (without
`--force`) or bad args.

### Emitted files

| File | Purpose |
|------|---------|
| `loopback-filter.types.ts` | `LoopbackFilter` / `LoopbackInclude` type declarations |
| `loopback-filter.converter.ts` | pure `toFindManyOptions()` + `LoopbackFilterError` (unit-testable, no NestJS) |
| `loopback-filter.pipe.ts` | `LoopbackFilterPipe` — a NestJS `PipeTransform` wrapping the converter |
| `index.ts` | barrel re-export |

### Wiring it into a controller

```ts
import { Controller, Get, Query } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { FindManyOptions, Repository } from 'typeorm';
import { LoopbackFilterPipe } from './common/lb';
import { CoffeeShop } from './coffee-shop.entity';

@Controller('coffeeShops')
export class CoffeeShopController {
  constructor(
    @InjectRepository(CoffeeShop)
    private readonly repo: Repository<CoffeeShop>,
  ) {}

  @Get()
  findAll(
    @Query('filter', LoopbackFilterPipe) options: FindManyOptions<CoffeeShop>,
  ) {
    return this.repo.find(options);
  }
}
```

An invalid `filter` (bad JSON, or an unsupported construct — see below) is
turned into a `400 Bad Request` by the pipe.

> **Requires `typeorm >= 0.3.17`** — the converter uses the `And()` combinator
> to express multiple operators on a single field.

## Filter → FindManyOptions mapping

| LoopBack `filter` key | TypeORM `FindManyOptions` |
|-----------------------|---------------------------|
| `where`  | `where` (object; an `or` produces a `where` **array** = OR) |
| `include` | `relations` (dot-notation string list) |
| `order`  | `order` (`{ prop: 'ASC' \| 'DESC' }`) |
| `limit`  | `take` |
| `skip` / `offset` | `skip` |
| `fields` | `select` |

### `where` operator mapping

| LoopBack | TypeORM | Notes |
|----------|---------|-------|
| `{prop: value}` | `prop: value` (or `Equal`) | primitive → equality; `null` → `IsNull()` |
| `gt`  | `MoreThan` | |
| `gte` | `MoreThanOrEqual` | |
| `lt`  | `LessThan` | |
| `lte` | `LessThanOrEqual` | |
| `between` | `Between(a, b)` | expects a two-element array |
| `inq` | `In(...)` | expects an array |
| `nin` | `Not(In(...))` | expects an array |
| `neq` | `Not(...)` | `neq: null` → `Not(IsNull())` |
| `like`  | `Like` | LoopBack SQL `LIKE` patterns (`%`, `_`) pass through unchanged |
| `nlike` | `Not(Like(...))` | |
| `ilike` | `ILike` | case-insensitive (Postgres) |
| `nilike`| `Not(ILike(...))` | |
| `like` + `{options: 'i'}` | `ILike` | the `options: 'i'` sibling flag upgrades `like`/`nlike` to `ILike` |
| `and: [...]` | merged AND clause | conditions combined into one object; a field repeated across the AND is joined with `And()` |
| `or: [...]`  | `where` array | TypeORM reads a `where` array as OR; nested `and`/`or` is flattened to disjunctive normal form |
| `{gt: 5, lt: 10}` (multiple ops on one field) | `And(MoreThan(5), LessThan(10))` | requires `typeorm >= 0.3.17` |

### Unsupported constructs (throw `LoopbackFilterError` → `400`)

- **`regexp`** — no portable TypeORM `FindOptions` equivalent. Use a
  `QueryBuilder` with a raw `WHERE` clause instead.
- **`near`** — geospatial ordering/proximity. Use a spatial query via
  `QueryBuilder`.
- **Exclusive `fields`** — `{ "password": false }` selects the *complement* of
  columns. TypeORM's `select` is an allow-list and can't express "everything
  except X"; list the fields to **keep** instead.
- **`include` `scope` filters** — only the relation graph carries over to
  `relations`. A scoped include (a `where`/`limit`/`order` applied to an
  included relation) has no `FindOptions` equivalent and is silently dropped;
  reach for `QueryBuilder` or `relationLoadStrategy` if you need it.

## Testing

```bash
python3 lb-filter/test_lb_filter.py
```

The emitted artifact is TypeScript, which isn't compiled here, so the tests
validate the **generator's output**: every operator mapping, class name, and
import is present; braces/parens/brackets balance in each `.ts` file; a
representative fixture's code paths exist; and the CLI's
write / `--force` / `--dry-run` / `--stdout` behaviour is correct.
