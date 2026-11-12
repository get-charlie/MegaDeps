# MegaDeps

Analyze the dependencies between the modules of a project: who imports whom, what is used and where, circular imports, an ASCII diagram, and unused code.

Works on Windows and Linux. Requires Python 3.8+, no dependencies. Supported languages: Python.

## Usage

```
python megadeps.py COMMAND [DIR] [options]
```

`DIR` defaults to the current directory and `COMMAND` defaults to `tree`.

| Command | Description |
|---|---|
| `tree` | Dependency tree: who imports whom. |
| `diagram` | Boxes and arrows drawn with text characters. |
| `cycles` | Circular dependencies, with the import lines that cause them. |
| `unused` | Unused functions, classes, methods and imports, plus unreachable code. |
| `check` | `cycles` + `unused`, for CI. |

```
$ python megadeps.py tree --uses
scripts.run_report
└── app.main  uses: run()
    ├── app.config  uses: load()
    │   └── app.utils  uses: slugify()
    │       └── app.config (lazy) (cycle)  uses: load()
    ├── app.db  uses: Session(), connect()
    │   └── app.utils (see above)  uses: retry(), slugify()
    ...

$ python megadeps.py diagram --root app.main
                          ┌──────────┐
                          │ app.main │
                          └─────┬────┘
                                │
            ┌─────────────────┬─┴────────────┬────┐
            ▼                 ▼              │    │
┌────────────┐  ┌──────────────────────┐     │    │
│ app.config │  │ app.services.billing │     │    │
└──────┬─────┘  └───────────┬──────────┘     │    │
       ...
```

Standard library modules are always ignored. Imports inside functions or under `if TYPE_CHECKING:` are marked as *lazy*.

### Options

| Option | Commands | Description |
|---|---|---|
| `--uses` | tree | Also show which functions/classes are used. |
| `--where` | tree | Also show where each one is used (file, line, function). |
| `--reverse` | tree | Show who depends on each module instead. |
| `--root MODULE` | tree, diagram | Start at this module (name, suffix or file path). |
| `--depth N` | tree | Maximum depth. |
| `--functions` | diagram | List the functions and classes inside each box. |
| `--all` | unused | Also show low-confidence results (decorated functions, inherited methods). |
| `--packages` | all | Group modules by package. |
| `--external` | all | Show third-party packages. |
| `--exclude-lazy` | all | Ignore lazy imports. |
| `--ascii` | all | Plain ASCII instead of Unicode box characters. |
| `--color auto\|always\|never` | all | Use colors (default: `auto`). |
| `--no-color` | all | Same as `--color never`. |
| `-i`, `--ignore PATTERN` | all | Glob pattern to ignore. Can be repeated. |
| `--no-default-ignores` | all | Do not ignore `.git`, `venv`, `build`, `dist`, etc. |

Unused code detection is static and heuristic: code only reached dynamically (plugins, `getattr` with computed names, public API of a library) may be reported as unused.

Exit code: `0` ok, `1` problems found (`cycles`, `unused`, `check`), `2` error.

## License

[MIT](LICENSE)
