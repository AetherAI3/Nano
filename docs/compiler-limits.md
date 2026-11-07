# Compiler resource limits

Source compilation rejects excessive nesting with `NanoResourceLimitError`, a
`NanoCompileError`/`NanoSyntaxError` carrying stable code `RESOURCE_LIMIT`, the
actual offending token's 1-based line and column, the resource name, and the
configured limit. The message asks the author to simplify the expression or
split the program. CLI text and editor diagnostics retain that message and
position. This does not change Python's process-global recursion limit or
convert internal exceptions to syntax errors.

The default and tested hard ceiling is **32**. Two counters use that ceiling:

- Active recursive syntax scopes: each `if` (including its guard and both
  branches), parenthesized expression, function-call argument scope, or index
  offset scope adds one. Strategy and schedule braces do not count. A `let`
  can contain 32 parenthesis levels; an outer `if` guard can contain 31.
- Expression tree depth: leaves have depth zero; unary, binary, call, index,
  and member nodes add one above their deepest child. A boolean chain of 33
  literal operands has depth 32. Comparisons also count: a chain of 32
  comparisons joined by 31 `and` operators reaches depth 32.

Syntax counters compose across constructs; they are not independent allowances
for parentheses and conditionals. Unary runs are parsed iteratively. Tree
depth is checked as nodes are constructed, including flat operator/postfix
chains, before recursive type checking or lowering receives the tree. The
first node that would exceed a limit is rejected at its own source position.
This preserves evaluation order and canonical IR for admitted programs.

Trusted hosts may lower the ceiling explicitly; no environment variable or
mutable global configuration participates in compilation:

```python
from nano.compiler import CompilerLimits, compile_to_dict

document = compile_to_dict(source, limits=CompilerLimits(max_nesting=16))
```

`parse`, `check_source`, `compile_to_dict`, `compile_module`, and
`compile_source` accept the same keyword. Only integers in `1..32` are accepted;
raising the ceiling requires a separately reviewed implementation and stack
measurements. The ceiling assumes the supported Python interpreter's normal
recursion budget and an ordinary host call stack; it does not reserve stack
space against arbitrary recursion in the embedding application.

This focused policy bounds **depth only**. It does not bound source bytes,
token count, total AST/emitted nodes, wide blocks, elapsed time, or total memory.
Hosts handling untrusted input should impose their own input-size/process
budgets. Those additional compiler budgets require separate work. Direct
construction of arbitrary AST/IR objects is outside source-parser admission.

## Reproducing the measurements

```text
py -3.11 scripts/compiler_limits_benchmark.py --repeats 25 --width 256 --output limits.json
```

The script measures lexing plus parsing at the admitted depth boundary for
parentheses, both unary forms, boolean/arithmetic chains, nested calls/indexes,
postfix members, mixed expressions, nested conditionals, and a 256-binding
wide program with every binding at the expression-depth limit. Timing uses
`perf_counter_ns`; memory is the peak Python allocation observed by
`tracemalloc` in a separate parse. It does not measure native allocations or
type checking/lowering. Because width is unbounded, there is no finite global
worst-case time or memory claim for all admitted sources.

Measured on 2026-11-01 with CPython 3.11.9, Windows 10 build 26200,
recursion limit 1000, 25 timed repetitions, and width 256. These are observed
fixture measurements on a shared machine, not latency guarantees.

| Admitted fixture | UTF-8 bytes | Median ms | Maximum ms | Peak traced bytes |
| --- | ---: | ---: | ---: | ---: |
| 32 parentheses | 99 | 0.4735 | 0.5157 | 21,878 |
| 32 unary `not` | 163 | 0.2494 | 0.2803 | 13,078 |
| 32 unary minus | 64 | 0.1940 | 0.2117 | 11,361 |
| 32 boolean operators | 323 | 0.5916 | 3.2023 | 22,198 |
| 32 arithmetic operators | 160 | 0.5278 | 0.7512 | 18,273 |
| 32 nested calls | 192 | 0.7819 | 0.9373 | 28,505 |
| 32 nested index offsets | 128 | 0.8157 | 4.5736 | 28,481 |
| 32 postfix members | 224 | 0.4320 | 1.5384 | 16,673 |
| 16 parentheses plus 32 unary operators | 195 | 0.5357 | 13.0268 | 23,038 |
| 32 conditionals | 425 | 1.0546 | 1.7245 | 38,024 |
| 256 bindings, each with 32 boolean operators | 78,756 | 285.6516 | 341.2899 | 5,726,657 |

Boundary regression tests exercise the default ceiling and a lower host limit,
exact-limit and one-over cases, 10,000-level generated attacks, mixed statement
and expression depth, all public source entry points, and CLI/editor agreement.
The existing compiler fuzz harness also runs generated resource boundaries
through compiler and editor adapters under each requested hash seed, recording
positions, codes, and any unexpected exception as evidence.
