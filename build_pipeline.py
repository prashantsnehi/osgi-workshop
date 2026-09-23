#!/usr/bin/env python3
"""
Northwind OMS OSGi Smart Build Pipeline
Handles:
  - Scenario 1: Full Build (all modules in dependency order)
  - Scenario 2: Incremental Build (changed modules + downstream dependencies)
"""

import argparse
import collections
import json
import os
import subprocess
import sys
from typing import Dict, List, Set, Tuple

# ==============================================================================
# OSGi Module Registry & Dependency Graph
# Define the path to each bundle/plugin and its required upstream modules
# ==============================================================================
MODULE_REGISTRY: Dict[str, Dict] = {
    "com.northwind.oms.core": {
        "path": "catalog/plugins/com.northwind.oms.core",
        "depends_on": [],
    },
    "com.northwind.oms.catalog": {
        "path": "catalog/plugins/com.northwind.oms.catalog",
        "depends_on": ["com.northwind.oms.core"],
    },
    "com.northwind.oms.inventory": {
        "path": "inventory/plugins/com.northwind.oms.inventory",
        "depends_on": ["com.northwind.oms.core", "com.northwind.oms.catalog"],
    },
    "com.northwind.oms.payment": {
        "path": "payment/plugins/com.northwind.oms.payment",
        "depends_on": ["com.northwind.oms.core"],
    },
    "com.northwind.oms.orders": {
        "path": "orders/plugins/com.northwind.oms.orders",
        "depends_on": [
            "com.northwind.oms.core",
            "com.northwind.oms.catalog",
            "com.northwind.oms.inventory",
            "com.northwind.oms.payment",
        ],
    },
    "com.northwind.oms.gateway": {
        "path": "orders/plugins/com.northwind.oms.gateway",
        "depends_on": ["com.northwind.oms.core", "com.northwind.oms.orders"],
    },
}


class OSGiBuildEngine:
    def __init__(self, registry: Dict[str, Dict]):
        self.registry = registry
        # Reverse map: upstream module -> list of downstream modules that require it
        self.downstream_graph: Dict[str, List[str]] = collections.defaultdict(list)
        for mod, meta in self.registry.items():
            for upstream in meta["depends_on"]:
                self.downstream_graph[upstream].append(mod)

    def topological_sort(self, target_modules: Set[str]) -> List[str]:
        """
        Topological sort using Kahn's Algorithm restricted to target_modules.
        Guarantees modules are built only after all upstream dependencies have completed.
        """
        in_degree = {m: 0 for m in target_modules}
        local_downstream = collections.defaultdict(list)

        for m in target_modules:
            for upstream in self.registry[m]["depends_on"]:
                if upstream in target_modules:
                    in_degree[m] += 1
                    local_downstream[upstream].append(m)

        queue = collections.deque([m for m, deg in in_degree.items() if deg == 0])
        ordered = []

        while queue:
            curr = queue.popleft()
            ordered.append(curr)
            for downstream in local_downstream[curr]:
                in_degree[downstream] -= 1
                if in_degree[downstream] == 0:
                    queue.append(downstream)

        if len(ordered) != len(target_modules):
            raise RuntimeError("Circular dependency detected in OSGi modules!")

        return ordered

    def detect_changes(self, base_ref: str, head_ref: str) -> Set[str]:
        """
        Detects modified modules via git diff against committed, staged, and unstaged changes.
        """
        changed_modules = set()
        modified_files = set()

        git_cmds = [
            ["git", "diff", "--name-only", f"{base_ref}...{head_ref}"],
            ["git", "diff", "--name-only", "HEAD"],
            ["git", "diff", "--name-only", "--staged"],
            ["git", "status", "--porcelain"],
        ]

        for cmd in git_cmds:
            try:
                res = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=True
                )
                for line in res.stdout.strip().splitlines():
                    if line:
                        clean_path = line[3:].strip() if len(line) > 3 and line[2] == " " else line.strip()
                        modified_files.add(clean_path.replace("\\", "/"))
            except subprocess.SubprocessError:
                continue

        for mod_id, meta in self.registry.items():
            path_prefix = meta["path"].strip("/").replace("\\", "/") + "/"
            for file_path in modified_files:
                if file_path.startswith(path_prefix):
                    changed_modules.add(mod_id)
                    break

        return changed_modules

    def resolve_transitive_dependencies(
        self, seed_modules: Set[str]
    ) -> Tuple[Set[str], Dict[str, Set[str]]]:
        """
        Computes transitive downstream impact:
        If module A is changed, any module B that depends on A must also be rebuilt.
        """
        to_rebuild = set(seed_modules)
        queue = collections.deque(seed_modules)
        dependency_chain = {m: set() for m in seed_modules}

        while queue:
            curr = queue.popleft()
            for downstream in self.downstream_graph.get(curr, []):
                if downstream not in dependency_chain:
                    dependency_chain[downstream] = set()
                dependency_chain[downstream].add(curr)

                if downstream not in to_rebuild:
                    to_rebuild.add(downstream)
                    queue.append(downstream)

        return to_rebuild, dependency_chain

    def compile_module(self, module_id: str) -> bool:
        """Simulates or invokes the build tool (e.g., Maven / Tycho / Gradle) for an OSGi module."""
        module_path = self.registry[module_id]["path"]
        print(f"  --> Building OSGi Bundle [{module_id}] at `{module_path}`...", flush=True)

        # Real build command integration:
        # Example for Maven/Tycho:
        # cmd = ["mvn", "clean", "package", "-DskipTests", "-f", os.path.join(module_path, "pom.xml")]
        # res = subprocess.run(cmd)
        # return res.returncode == 0
        return True


# ==============================================================================
# Main Orchestrator
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="Northwind OMS OSGi Build Pipeline")
    parser.add_argument("--mode", choices=["all", "changed"], required=True, help="Build scenario")
    parser.add_argument("--base", default="HEAD~1", help="Base commit/branch for git diff")
    parser.add_argument("--head", default="HEAD", help="Head commit/branch for git diff")
    args = parser.parse_args()

    engine = OSGiBuildEngine(MODULE_REGISTRY)

    print("=" * 70)
    print(f"  NORTHWIND OMS OSGi SMART BUILD PIPELINE")
    print(f"  SCENARIO: {'1 (BUILD ALL PRODUCTS)' if args.mode == 'all' else '2 (BUILD ONLY CHANGED/IMPACTED)'}")
    print("=" * 70)

    if args.mode == "all":
        target_modules = set(MODULE_REGISTRY.keys())
        build_order = engine.topological_sort(target_modules)
        changed_seeds = target_modules
        indirect_impact = set()
        dep_chains = {}
    else:
        changed_seeds = engine.detect_changes(args.base, args.head)
        if not changed_seeds:
            print("\n[✓] No changes detected in any OSGi bundles. Nothing to build.")
            if "GITHUB_OUTPUT" in os.environ:
                with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                    f.write("has_changes=false\n")
            sys.exit(0)

        target_modules, dep_chains = engine.resolve_transitive_dependencies(changed_seeds)
        indirect_impact = target_modules - changed_seeds
        build_order = engine.topological_sort(target_modules)

    # 1. Log Detected Changed Modules
    print(f"\n[Step 1] Detected Changed Modules ({len(changed_seeds)}):")
    for mod in sorted(changed_seeds):
        status = "Direct Change" if args.mode == "changed" else "Full Build Selection"
        print(f"  • {mod:<32} [{status}] -> {MODULE_REGISTRY[mod]['path']}")

    # 2. Log Complete Dependency Chain Resolution
    print(f"\n[Step 2] Complete Dependency Chain Resolution:")
    if indirect_impact:
        print(f"  Transitively Impacted Bundles ({len(indirect_impact)}):")
        for mod in sorted(indirect_impact):
            causes = ", ".join(sorted(dep_chains[mod]))
            print(f"  • {mod:<32} (Triggered by upstream changes in: {causes})")
    elif args.mode == "changed":
        print("  • No indirect downstream bundles impacted.")
    else:
        print("  • Full repository graph traversal selected.")

    # 3. Log Ordered Build Plan
    print(f"\n[Step 3] Topological Build Order ({len(build_order)} modules):")
    plan_payload = []
    for idx, mod in enumerate(build_order, start=1):
        wait_deps = [d for d in MODULE_REGISTRY[mod]["depends_on"] if d in target_modules]
        dep_info = f"Waits for: {', '.join(wait_deps)}" if wait_deps else "Root Bundle (no dependencies)"
        print(f"  {idx}. {mod:<32} | {dep_info}")
        plan_payload.append({
            "order": idx,
            "id": mod,
            "path": MODULE_REGISTRY[mod]["path"],
            "type": "Direct" if mod in changed_seeds else "Transitive",
        })

    # 4. Confirmation of Selective Rebuilding
    all_modules = set(MODULE_REGISTRY.keys())
    skipped_modules = all_modules - target_modules
    print(f"\n[Step 4] Rebuild Scope Verification:")
    print(f"  • Modules Scheduled: {len(target_modules)} / {len(all_modules)}")
    if skipped_modules:
        print(f"  • Modules Skipped (Unaffected): {', '.join(sorted(skipped_modules))}")
    else:
        print(f"  • All modules require rebuild.")

    # 5. Build Execution Phase
    print(f"\n[Step 5] Executing Bundle Compilation:")
    for mod in build_order:
        success = engine.compile_module(mod)
        if not success:
            print(f"\n[!] Build failed at bundle: {mod}")
            sys.exit(1)

    print("\n" + "=" * 70)
    print("  [SUCCESS] All scheduled OSGi bundles built in valid topological order.")
    print("=" * 70)

    # Set GitHub Actions output parameters and summary
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("has_changes=true\n")
            f.write(f"build_order={json.dumps(plan_payload)}\n")

    if "GITHUB_STEP_SUMMARY" in os.environ:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("### OSGi Build Execution Report\n\n")
            f.write(f"- **Mode:** `{args.mode}`\n")
            f.write(f"- **Total Rebuilt:** `{len(target_modules)}` / `{len(all_modules)}`\n\n")
            f.write("| Order | Bundle ID | Path | Trigger Reason |\n")
            f.write("| :--- | :--- | :--- | :--- |\n")
            for item in plan_payload:
                f.write(f"| {item['order']} | `{item['id']}` | `{item['path']}` | {item['type']} |\n")


if __name__ == "__main__":
    main()