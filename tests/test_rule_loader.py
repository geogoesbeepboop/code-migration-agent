"""Tests for the rule loader and rule index."""

from migration.profiles import load_profile
from migration.rule_loader import load_rule_index


class TestRuleLoader:
    def setup_method(self):
        profile = load_profile("java_to_kotlin")
        self.rule_index = load_rule_index(profile.rules_path)

    def test_all_rules_loaded(self):
        assert len(self.rule_index.rules) == 13
        ids = {r.rule_id for r in self.rule_index.rules}
        for expected in ["R01", "R02", "R03", "R04", "R05", "R13"]:
            assert expected in ids

    def test_lombok_fires_r02(self):
        imports = ["lombok.Data"]
        fired = self.rule_index.rules_for_file(imports, "")
        assert "R02" in fired

    def test_null_pattern_fires_r01(self):
        fired = self.rule_index.rules_for_file([], "private String name = null;")
        assert "R01" in fired

    def test_autowired_fires_r13(self):
        imports = ["org.springframework.beans.factory.annotation.Autowired"]
        fired = self.rule_index.rules_for_file(imports, "")
        assert "R13" in fired

    def test_switch_fires_r05(self):
        fired = self.rule_index.rules_for_file([], "switch (day) { case MONDAY: break; }")
        assert "R05" in fired

    def test_rule_text_renders(self):
        text = self.rule_index.rule_text(["R01", "R02"])
        assert "R01" in text
        assert "R02" in text
        assert "Pattern:" in text

    def test_empty_source_minimal_rules(self):
        fired = self.rule_index.rules_for_file([], "public class Empty {}")
        # An empty class shouldn't trigger many rules
        assert len(fired) <= 2
