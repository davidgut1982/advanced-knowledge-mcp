#!/usr/bin/env python3
"""
PM Knowledge Integration Validation Script
Tests that KB and OB1 search requirements are properly integrated into PM instructions
"""

import subprocess
import sys
from pathlib import Path

def test_pm_instructions_exist():
    """Verify PM instructions file exists and has required KB/OB1 integration"""
    pm_file = Path("/home/david/advanced-knowledge-mcp/docs/PM_INSTRUCTIONS.md")

    if not pm_file.exists():
        return False, f"PM instructions file missing: {pm_file}"

    with open(pm_file, 'r') as f:
        content = f.read()

    return True, "PM instructions file found"

def test_kb_integration():
    """Verify KB search integration is present"""
    pm_file = Path("/home/david/advanced-knowledge-mcp/docs/PM_INSTRUCTIONS.md")

    with open(pm_file, 'r') as f:
        content = f.read()

    required_kb_elements = [
        "mcp__knowledge-mcp__kb_search",
        "mcp__knowledge-mcp__multi_search",
        "KNOWLEDGE SYSTEM SEARCH",
        "Search Knowledge Base",
    ]

    missing_elements = []
    for element in required_kb_elements:
        if element not in content:
            missing_elements.append(element)

    if missing_elements:
        return False, f"Missing KB integration elements: {missing_elements}"

    return True, "KB integration validated"

def test_ob1_integration():
    """Verify OB1 memory integration is present"""
    pm_file = Path("/home/david/advanced-knowledge-mcp/docs/PM_INSTRUCTIONS.md")

    with open(pm_file, 'r') as f:
        content = f.read()

    required_ob1_elements = [
        "mcp__ob1-memory__search_thoughts",
        "Search OB1 Memory",
        "OB1 memory search",
    ]

    missing_elements = []
    for element in required_ob1_elements:
        if element not in content:
            missing_elements.append(element)

    if missing_elements:
        return False, f"Missing OB1 integration elements: {missing_elements}"

    return True, "OB1 integration validated"

def test_workflow_integration():
    """Verify workflow integration includes knowledge search as first step"""
    pm_file = Path("/home/david/advanced-knowledge-mcp/docs/PM_INSTRUCTIONS.md")

    with open(pm_file, 'r') as f:
        content = f.read()

    workflow_elements = [
        "KB/OB1 SEARCH → PRE-ACTION VERIFICATION",
        "Modified Decision Flow",
        "Step 1: KNOWLEDGE SYSTEM SEARCH (NEW - MANDATORY)",
    ]

    missing_elements = []
    for element in workflow_elements:
        if element not in content:
            missing_elements.append(element)

    if missing_elements:
        return False, f"Missing workflow integration elements: {missing_elements}"

    return True, "Workflow integration validated"

def test_circuit_breaker_integration():
    """Verify circuit breaker for missing KB/OB1 search"""
    pm_file = Path("/home/david/advanced-knowledge-mcp/docs/PM_INSTRUCTIONS.md")

    with open(pm_file, 'r') as f:
        content = f.read()

    circuit_breaker_elements = [
        "Circuit Breaker #14",
        "PM making decisions without KB/OB1 search",
        "Any major decision made without documented KB and OB1 search",
    ]

    missing_elements = []
    for element in circuit_breaker_elements:
        if element not in content:
            missing_elements.append(element)

    if missing_elements:
        return False, f"Missing circuit breaker elements: {missing_elements}"

    return True, "Circuit breaker integration validated"

def test_evidence_requirements():
    """Verify evidence requirements include KB/OB1 context"""
    pm_file = Path("/home/david/advanced-knowledge-mcp/docs/PM_INSTRUCTIONS.md")

    with open(pm_file, 'r') as f:
        content = f.read()

    evidence_elements = [
        "KB/OB1 Context: [relevant search results that informed decision]",
        "KB search showed this aligns with previous update patterns",
        "OB1 memory search revealed similar issue resolution",
    ]

    missing_elements = []
    for element in evidence_elements:
        if element not in content:
            missing_elements.append(element)

    if missing_elements:
        return False, f"Missing evidence requirement elements: {missing_elements}"

    return True, "Evidence requirements validated"

def test_validation_checklist():
    """Verify validation checklist includes KB/OB1 requirements"""
    pm_file = Path("/home/david/advanced-knowledge-mcp/docs/PM_INSTRUCTIONS.md")

    with open(pm_file, 'r') as f:
        content = f.read()

    checklist_elements = [
        "KB search completed with query",
        "OB1 memory search completed with query",
        "Search results documented and analyzed",
        "Decision rationale includes knowledge system findings",
    ]

    missing_elements = []
    for element in checklist_elements:
        if element not in content:
            missing_elements.append(element)

    if missing_elements:
        return False, f"Missing checklist elements: {missing_elements}"

    return True, "Validation checklist complete"

def run_validation():
    """Run complete KB/OB1 integration validation"""
    tests = [
        ("PM Instructions Exist", test_pm_instructions_exist),
        ("KB Integration", test_kb_integration),
        ("OB1 Integration", test_ob1_integration),
        ("Workflow Integration", test_workflow_integration),
        ("Circuit Breaker Integration", test_circuit_breaker_integration),
        ("Evidence Requirements", test_evidence_requirements),
        ("Validation Checklist", test_validation_checklist),
    ]

    print("🧪 Validating PM Knowledge Integration...")
    print("=" * 50)

    all_passed = True
    for test_name, test_func in tests:
        try:
            passed, message = test_func()
            status = "✅ PASS" if passed else "❌ FAIL"
            print(f"{status:8} | {test_name:25} | {message}")
            all_passed = all_passed and passed
        except Exception as e:
            print(f"❌ ERROR  | {test_name:25} | Exception: {e}")
            all_passed = False

    print("=" * 50)
    if all_passed:
        print("🎉 PM Knowledge Integration validation: PASSED")
        print("\n📋 SUCCESS CRITERIA MET:")
        print("   ✅ KB search mandatory before decisions")
        print("   ✅ OB1 memory search mandatory before decisions")
        print("   ✅ Circuit breaker for missing knowledge search")
        print("   ✅ Evidence requirements include knowledge context")
        print("   ✅ Workflow integration complete")
        return 0
    else:
        print("❌ PM Knowledge Integration validation: FAILED")
        print("\n🚨 ISSUES FOUND:")
        print("   Integration incomplete - fix issues above")
        return 1

if __name__ == "__main__":
    sys.exit(run_validation())