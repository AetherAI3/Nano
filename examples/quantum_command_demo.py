"""Phrase -> typed operations -> actual local samples -> structured reply.

Run from an installed checkout: python examples/quantum_command_demo.py
Requires .[quantum]. No model, credentials, network, IBM runtime, or paid call.
"""
import json
from importlib.resources import files

from nano.intent import compile_intent
from nano.quantum import build_plan, prepare_bits, sample
from nano_qsearch.quantum_commands import run_local_command


def demo() -> dict:
    catalog = json.loads(files("nano.intent").joinpath("fixtures/quantum_commands_v1.json").read_text(encoding="utf-8"))["catalog"]
    plan = build_plan([
        prepare_bits("100"),
        sample("prepare", expected_width=3, shots=100, max_circuits=1,
               max_wall_s=15, max_qpu_s=0),
    ])
    catalog["plans"]["demo-plan"].update(operation_ref="sample", admitted_preparation_ref="prepare",
                                           operation_digest=plan.plan_digest)
    source = "simulate quantum plan demo-plan on local with 100 shots 1 circuit 0 qpu seconds"
    command = compile_intent(source, profile="quantum", quantum_catalog=catalog)
    reply = run_local_command(command, catalog=catalog, plans={"plan:demo": plan}, evidence_catalog={},
                              backend_bindings={"local_simulator": "local_ideal"})
    return {"source": source, "command": command.to_dict(), "typed_plan": plan.to_payload(), "reply": reply}


if __name__ == "__main__":
    print(json.dumps(demo(), indent=2, ensure_ascii=False))
