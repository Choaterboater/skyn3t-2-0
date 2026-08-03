"""Native iOS SwiftUI stack contract."""
from __future__ import annotations

from skyn3t.agents._common import KNOWN_STACKS, _normalize_stack
from skyn3t.agents._common import detect_stack as agent_detect_stack
from skyn3t.agents._scaffold import scaffold_for
from skyn3t.studio.planner import detect_stack as plan_detect_stack
from skyn3t.studio.planner import file_checklist
from skyn3t.studio.proof_run import _NODE_STACKS, _SWIFT_IOS_STACKS, proof_run
from skyn3t.studio.stack_selector import REAL_BUILDER_STACKS, classify_build


def test_native_ios_swift_routes_before_expo() -> None:
    brief = "Build a native iOS wine cellar app using SwiftUI with bottle scanning"
    assert plan_detect_stack(brief) == "swift_ios"
    assert agent_detect_stack(brief=brief) == "swift_ios"
    assert plan_detect_stack("an iOS app with a login screen") == "react_native"


def test_native_ios_aliases_and_classification() -> None:
    assert "swift_ios" in KNOWN_STACKS
    for alias in ("swift_ios", "ios swift", "swiftui_ios", "native ios"):
        assert _normalize_stack(alias) == "swift_ios"
    result = classify_build("a Swift iPhone app", "swift_ios")
    assert (result.app_type, result.engine) == ("mobile_app", "swiftui_ios")


def test_scaffold_has_xcode_scanner_and_manual_inventory() -> None:
    files = scaffold_for("swift_ios", "cellar-companion", "A wine cellar inventory app")
    for path in (
        "App.xcodeproj/project.pbxproj", "App/App.swift", "App/ContentView.swift",
        "App/BottleScannerView.swift", "App/BottleDetailView.swift", "App/BottleEditorView.swift", "App/Providers.swift", "App/Models.swift", "App/Info.plist",
        "AppTests/AppTests.swift", "README.md",
    ):
        assert path in files, path
    source = "\n".join(text for path, text in files.items() if path.endswith(".swift"))
    for token in ("SwiftData", "DataScannerViewController", "manual", "ReviewProvider", "purchasePrice", "estimatedValue", "MarketValueProvider", "TastingReview", "ShareLink"):
        assert token in source, token
    assert "NSCameraUsageDescription" in files["App/Info.plist"]
    assert "PBXNativeTarget" in files["App.xcodeproj/project.pbxproj"]
    assert "package.json" not in files


def test_scaffold_proves_structurally_without_xcode(tmp_path) -> None:
    for relative, text in scaffold_for("swift_ios", "cellar-companion", "A wine cellar inventory app").items():
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)
    result = proof_run(tmp_path, checklist=file_checklist("swift_ios"), stack="swift_ios")
    assert result.passed, result.to_dict()
    assert "swift_ios" in _SWIFT_IOS_STACKS
    assert "swift_ios" not in _NODE_STACKS
    assert "swift_ios" in REAL_BUILDER_STACKS
