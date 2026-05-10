"""Validate skill files reference correct paths and structure."""

from __future__ import annotations

import re
from pathlib import Path

SKILLS_DIR = Path(__file__).parent.parent.parent.parent / "src" / "skills"


class TestReviewCodeSkill:
    """Code review skill (forge:review)."""

    def test_skill_exists(self):
        assert (SKILLS_DIR / "review" / "SKILL.md").exists()

    def test_name_is_review(self):
        content = (SKILLS_DIR / "review" / "SKILL.md").read_text()
        assert "name: forge:review" in content

    def test_no_mode_detection(self):
        """Auto-detection removed; always code mode."""
        content = (SKILLS_DIR / "review" / "SKILL.md").read_text()
        assert "--mode" not in content

    def test_no_codereview_resource(self):
        """Duplicate codereview.md deleted; canonical copy in forge.review.resources."""
        assert not (SKILLS_DIR / "review" / "resources" / "codereview.md").exists()

    def test_auto_detects_model_family(self):
        content = (SKILLS_DIR / "review" / "SKILL.md").read_text()
        assert "forge session context" in content
        assert "model_family" in content

    def test_no_style_flag(self):
        content = (SKILLS_DIR / "review" / "SKILL.md").read_text()
        assert "--style" not in content

    def test_code_resources_exist(self):
        resources = SKILLS_DIR / "review" / "resources"
        assert (resources / "code.md").exists()
        assert (resources / "code-openai.md").exists()
        assert (resources / "code-gemini.md").exists()

    def test_references_exist(self):
        refs = SKILLS_DIR / "review" / "references"
        assert (refs / "claude-4.6.md").exists()
        assert (refs / "claude-4.7.md").exists()
        assert (refs / "gpt-5.5.md").exists()
        assert (refs / "gemini-3.1.md").exists()

    def test_model_guides_are_standalone(self):
        refs = SKILLS_DIR / "review" / "references"
        guide_names = ("claude-4.6.md", "claude-4.7.md", "gpt-5.5.md", "gemini-3.1.md")
        cross_guide_phrases = (
            "4.6 guide",
            "4.7 guide",
            "gpt-5.5 guide",
            "gemini 3.1 guide",
            "other guide",
            "companion guide",
            "reference set",
        )
        local_md_link = re.compile(r"\[[^\]]+\]\((?!https?://)[^)]+\.md(?:#[^)]+)?\)")

        for guide_name in guide_names:
            content = (refs / guide_name).read_text()
            lower_content = content.lower()

            assert not local_md_link.search(content), f"{guide_name} links to another local Markdown file"

            for other_guide in guide_names:
                assert other_guide not in content, f"{guide_name} references {other_guide}"

            for phrase in cross_guide_phrases:
                assert phrase not in lower_content, f"{guide_name} contains cross-guide phrase: {phrase}"


class TestModelDocDrift:
    """Lightweight checks for model-name and reasoning-effort drift in skills."""

    def test_multi_model_skills_reference_current_gpt_proxy_requirement(self):
        expected = "GPT-5.5 and Gemini require active proxies"
        stale = "GPT-5.4 and Gemini require active proxies"

        for skill_name in ("panel", "debate", "consensus"):
            content = (SKILLS_DIR / skill_name / "SKILL.md").read_text()
            assert expected in content
            assert stale not in content

    def test_gpt_55_parallel_tool_note_uses_supported_reasoning_effort(self):
        content = (SKILLS_DIR / "review" / "references" / "gpt-5.5.md").read_text()
        parallel_section = content.split("### Parallel Tool Calls", 1)[1].split("---", 1)[0]

        assert "`none`" in parallel_section
        assert "`minimal`" not in parallel_section


class TestReviewDocsSkill:
    """Document review skill (forge:review-docs)."""

    def test_skill_exists(self):
        assert (SKILLS_DIR / "review-docs" / "SKILL.md").exists()

    def test_name_is_review_docs(self):
        content = (SKILLS_DIR / "review-docs" / "SKILL.md").read_text()
        assert "name: forge:review-docs" in content

    def test_docs_resources_exist(self):
        resources = SKILLS_DIR / "review-docs" / "resources"
        assert (resources / "docs.md").exists()
        assert (resources / "docs-openai.md").exists()
        assert (resources / "docs-gemini.md").exists()

    def test_auto_detects_model_family(self):
        content = (SKILLS_DIR / "review-docs" / "SKILL.md").read_text()
        assert "forge session context" in content
        assert "model_family" in content


class TestOldSkillsDeleted:
    """Old skill directories must not exist."""

    def test_review_code_deleted(self):
        assert not (SKILLS_DIR / "review-code").exists()

    def test_review_design_deleted(self):
        assert not (SKILLS_DIR / "review-design").exists()

    def test_ensemble_deleted(self):
        assert not (SKILLS_DIR / "ensemble").exists()

    def test_thinkdeep_deleted(self):
        assert not (SKILLS_DIR / "thinkdeep").exists()


class TestPanelSkill:
    def test_skill_exists(self):
        assert (SKILLS_DIR / "panel" / "SKILL.md").exists()

    def test_name_is_panel(self):
        content = (SKILLS_DIR / "panel" / "SKILL.md").read_text()
        assert "name: forge:panel" in content

    def test_references_forge_workflow(self):
        content = (SKILLS_DIR / "panel" / "SKILL.md").read_text()
        assert "forge workflow panel" in content

    def test_no_resource_flag(self):
        content = (SKILLS_DIR / "panel" / "SKILL.md").read_text()
        assert "--resource" not in content

    def test_synthesis_resource_exists(self):
        resource = SKILLS_DIR / "panel" / "resources" / "synthesis.md"
        assert resource.exists()

    def test_step1_parses_flags(self):
        """Step 1 must list all CLI flags for the agent to extract."""
        content = (SKILLS_DIR / "panel" / "SKILL.md").read_text()
        for flag in ("--code", "--models", "--roles", "--review-type", "--severity"):
            assert flag in content, f"Panel SKILL.md missing {flag} in Step 1"

    def test_step2_forwards_flags(self):
        """Step 2 bash command must forward all parsed flags."""
        content = (SKILLS_DIR / "panel" / "SKILL.md").read_text()
        step2 = content.split("### Step 2")[1].split("### Step 3")[0]
        for flag in ("--roles", "--review-type", "--severity"):
            assert flag in step2, f"Panel Step 2 doesn't forward {flag}"

    def test_argument_hint_includes_new_flags(self):
        content = (SKILLS_DIR / "panel" / "SKILL.md").read_text()
        # Frontmatter is between first and second ---
        frontmatter = content.split("---")[1]
        for flag in ("--roles", "--review-type", "--severity"):
            assert flag in frontmatter, f"Panel argument-hint missing {flag}"


class TestAnalyzeSkill:
    def test_skill_exists(self):
        assert (SKILLS_DIR / "analyze" / "SKILL.md").exists()

    def test_name_is_analyze(self):
        content = (SKILLS_DIR / "analyze" / "SKILL.md").read_text()
        assert "name: forge:analyze" in content

    def test_references_forge_workflow_analyze(self):
        content = (SKILLS_DIR / "analyze" / "SKILL.md").read_text()
        assert "forge workflow analyze" in content

    def test_no_resource(self):
        """Analyze has no local resources (framework loaded by CLI)."""
        resources = SKILLS_DIR / "analyze" / "resources"
        assert not resources.exists() or not list(resources.iterdir())

    def test_step1_parses_models_flag(self):
        content = (SKILLS_DIR / "analyze" / "SKILL.md").read_text()
        assert "--models" in content.split("### Step 2")[0]

    def test_step2_forwards_models_flag(self):
        content = (SKILLS_DIR / "analyze" / "SKILL.md").read_text()
        step2 = content.split("### Step 2")[1].split("### Step 3")[0]
        assert "--models" in step2


class TestDebateSkill:
    def test_skill_exists(self):
        assert (SKILLS_DIR / "debate" / "SKILL.md").exists()

    def test_name_is_debate(self):
        content = (SKILLS_DIR / "debate" / "SKILL.md").read_text()
        assert "name: forge:debate" in content

    def test_references_forge_workflow_debate(self):
        content = (SKILLS_DIR / "debate" / "SKILL.md").read_text()
        assert "forge workflow debate" in content

    def test_no_resource_flag(self):
        """Debate skill no longer uses --resource (CLI handles template)."""
        content = (SKILLS_DIR / "debate" / "SKILL.md").read_text()
        assert "--resource" not in content

    def test_blocks_auto_invocation(self):
        """Debate is expensive (multi-model) -- must not auto-invoke."""
        content = (SKILLS_DIR / "debate" / "SKILL.md").read_text()
        assert "disable-model-invocation: true" in content

    def test_resource_exists(self):
        assert (SKILLS_DIR / "debate" / "resources" / "debate_evaluation.md").exists()

    def test_resource_contains_stance_marker(self):
        content = (SKILLS_DIR / "debate" / "resources" / "debate_evaluation.md").read_text()
        assert "{stance_prompt}" in content

    def test_resource_contains_proposal_placeholder(self):
        content = (SKILLS_DIR / "debate" / "resources" / "debate_evaluation.md").read_text()
        assert "{proposal}" in content

    def test_code_resource_exists(self):
        assert (SKILLS_DIR / "debate" / "resources" / "code_debate_evaluation.md").exists()

    def test_code_resource_contains_stance_marker(self):
        content = (SKILLS_DIR / "debate" / "resources" / "code_debate_evaluation.md").read_text()
        assert "{stance_prompt}" in content

    def test_code_resource_contains_target_placeholder(self):
        content = (SKILLS_DIR / "debate" / "resources" / "code_debate_evaluation.md").read_text()
        assert "{target}" in content

    def test_step1_parses_worker_flag(self):
        content = (SKILLS_DIR / "debate" / "SKILL.md").read_text()
        assert "--worker" in content.split("### Step 2")[0]

    def test_step2_forwards_worker_flag(self):
        content = (SKILLS_DIR / "debate" / "SKILL.md").read_text()
        step2 = content.split("### Step 2")[1].split("### Step 3")[0]
        assert "--worker" in step2

    def test_argument_hint_includes_worker(self):
        content = (SKILLS_DIR / "debate" / "SKILL.md").read_text()
        frontmatter = content.split("---")[1]
        assert "--worker" in frontmatter


class TestUnderstandSkill:
    def test_skill_exists(self):
        assert (SKILLS_DIR / "understand" / "SKILL.md").exists()

    def test_auto_detects_model_family(self):
        content = (SKILLS_DIR / "understand" / "SKILL.md").read_text()
        assert "forge session context" in content
        assert "model_family" in content

    def test_allows_bash_for_model_detection(self):
        content = (SKILLS_DIR / "understand" / "SKILL.md").read_text()
        assert "allowed-tools: Read, Grep, Glob, Bash" in content

    def test_code_resources_exist(self):
        resources = SKILLS_DIR / "understand" / "resources"
        assert (resources / "code.md").exists()
        assert (resources / "code-openai.md").exists()
        assert (resources / "code-gemini.md").exists()

    def test_docs_resources_exist(self):
        resources = SKILLS_DIR / "understand" / "resources"
        assert (resources / "docs.md").exists()
        assert (resources / "docs-openai.md").exists()
        assert (resources / "docs-gemini.md").exists()


class TestChallengeSkill:
    """Challenge skill (forge:challenge)."""

    def test_skill_exists(self):
        assert (SKILLS_DIR / "challenge" / "SKILL.md").exists()

    def test_name_is_challenge(self):
        content = (SKILLS_DIR / "challenge" / "SKILL.md").read_text()
        assert "name: forge:challenge" in content

    def test_is_model_invocable(self):
        """Challenge must be auto-invocable (no disable-model-invocation)."""
        content = (SKILLS_DIR / "challenge" / "SKILL.md").read_text()
        assert "disable-model-invocation" not in content

    def test_has_read_only_tools(self):
        """Challenge is evaluative -- no Write or Edit access."""
        content = (SKILLS_DIR / "challenge" / "SKILL.md").read_text()
        assert "allowed-tools:" in content
        assert "Write" not in content
        assert "Edit" not in content

    def test_defaults_to_skepticism(self):
        content = (SKILLS_DIR / "challenge" / "SKILL.md").read_text()
        assert "skepticism" in content.lower()

    def test_infers_from_context_when_empty(self):
        """Empty args should infer from conversation, not default to cwd."""
        content = (SKILLS_DIR / "challenge" / "SKILL.md").read_text()
        assert "infer" in content.lower()
        assert "current working directory" not in content.lower()


class TestQaWorkflowChecklist:
    def test_session_context_step_uses_positional_session_arg(self):
        skills_md = SKILLS_DIR / "qa" / "resources" / "checklist" / "15-skills.md"
        content = skills_md.read_text()
        step = content.split("### 15.1", 1)[1].split("### 15.2", 1)[0]
        assert "forge session context test-session-1 --json" in step
        assert "--session test-session-1" not in step

    def test_live_debate_step_uses_real_slash_command(self):
        review_md = SKILLS_DIR / "qa" / "resources" / "checklist" / "14-workflow.md"
        content = review_md.read_text()
        step = content.split("### 14.10", 1)[1].split("\n---", 1)[0]
        assert "/forge:debate" in step
        assert "Do not type `/forge:debate`" not in step

    def test_live_debate_step_is_display_only(self):
        """Regression for QA-036: guided step must not look directly runnable."""
        review_md = SKILLS_DIR / "qa" / "resources" / "checklist" / "14-workflow.md"
        content = review_md.read_text()
        step = content.split("### 14.10", 1)[1].split("\n### ", 1)[0]
        assert "```bash" not in step


class TestConsensusSkill:
    def test_skill_exists(self):
        assert (SKILLS_DIR / "consensus" / "SKILL.md").exists()

    def test_name_is_consensus(self):
        content = (SKILLS_DIR / "consensus" / "SKILL.md").read_text()
        assert "name: forge:consensus" in content

    def test_references_forge_workflow_consensus(self):
        content = (SKILLS_DIR / "consensus" / "SKILL.md").read_text()
        assert "forge workflow consensus" in content

    def test_blocks_auto_invocation(self):
        """Consensus is expensive (multi-model, two rounds) -- must not auto-invoke."""
        content = (SKILLS_DIR / "consensus" / "SKILL.md").read_text()
        assert "disable-model-invocation: true" in content

    def test_resource_exists(self):
        assert (SKILLS_DIR / "consensus" / "resources" / "consensus_evaluation.md").exists()

    def test_resource_contains_role_marker(self):
        content = (SKILLS_DIR / "consensus" / "resources" / "consensus_evaluation.md").read_text()
        assert "{role_prompt}" in content

    def test_resource_contains_subject_placeholder(self):
        content = (SKILLS_DIR / "consensus" / "resources" / "consensus_evaluation.md").read_text()
        assert "{subject}" in content

    def test_code_resource_exists(self):
        assert (SKILLS_DIR / "consensus" / "resources" / "code_consensus_evaluation.md").exists()

    def test_code_resource_contains_role_marker(self):
        content = (SKILLS_DIR / "consensus" / "resources" / "code_consensus_evaluation.md").read_text()
        assert "{role_prompt}" in content

    def test_code_resource_contains_target_placeholder(self):
        content = (SKILLS_DIR / "consensus" / "resources" / "code_consensus_evaluation.md").read_text()
        assert "{target}" in content

    def test_synthesis_resource_exists(self):
        assert (SKILLS_DIR / "consensus" / "resources" / "synthesis.md").exists()

    def test_step1_parses_worker_flag(self):
        content = (SKILLS_DIR / "consensus" / "SKILL.md").read_text()
        assert "--worker" in content.split("### Step 2")[0]

    def test_step2_forwards_worker_flag(self):
        content = (SKILLS_DIR / "consensus" / "SKILL.md").read_text()
        step2 = content.split("### Step 2")[1].split("### Step 3")[0]
        assert "--worker" in step2

    def test_argument_hint_includes_worker(self):
        content = (SKILLS_DIR / "consensus" / "SKILL.md").read_text()
        frontmatter = content.split("---")[1]
        assert "--worker" in frontmatter

    def test_uses_support_not_accept_vocabulary(self):
        """Consensus templates must use SUPPORT/OPPOSE, not ACCEPT/REJECT."""
        for name in ("consensus_evaluation.md", "code_consensus_evaluation.md"):
            content = (SKILLS_DIR / "consensus" / "resources" / name).read_text()
            assert "SUPPORT" in content
            assert "OPPOSE" in content
            assert '"ACCEPT"' not in content
            assert '"REJECT"' not in content


class TestConsensusTemplateEquivalence:
    """CLI-embedded templates must match canonical skill resource copies."""

    def test_proposal_template_matches(self):
        from forge.cli.workflow import _CONSENSUS_EVALUATION_TEMPLATE

        canonical = (SKILLS_DIR / "consensus" / "resources" / "consensus_evaluation.md").read_text()
        assert _CONSENSUS_EVALUATION_TEMPLATE == canonical

    def test_code_template_matches(self):
        from forge.cli.workflow import _CODE_CONSENSUS_EVALUATION_TEMPLATE

        canonical = (SKILLS_DIR / "consensus" / "resources" / "code_consensus_evaluation.md").read_text()
        assert _CODE_CONSENSUS_EVALUATION_TEMPLATE == canonical


class TestDebateTemplateEquivalence:
    """CLI-embedded templates must match canonical skill resource copies."""

    def test_proposal_template_matches(self):
        from forge.cli.workflow import _DEBATE_EVALUATION_TEMPLATE

        canonical = (SKILLS_DIR / "debate" / "resources" / "debate_evaluation.md").read_text()
        assert _DEBATE_EVALUATION_TEMPLATE == canonical

    def test_code_template_matches(self):
        from forge.cli.workflow import _CODE_DEBATE_EVALUATION_TEMPLATE

        canonical = (SKILLS_DIR / "debate" / "resources" / "code_debate_evaluation.md").read_text()
        assert _CODE_DEBATE_EVALUATION_TEMPLATE == canonical


class TestQaHandoffChecklist:
    def test_handoff_setup_sets_designated_docs(self):
        handoff_md = SKILLS_DIR / "qa" / "resources" / "checklist" / "16-handoff.md"
        content = handoff_md.read_text()
        step = content.split("### 16.1", 1)[1].split("### 16.2", 1)[0]
        assert "memory.designated_docs" in step
        assert '".forge/memory/debugging.md"' in step
        assert '".forge/memory/patterns.md"' in step

    def test_handoff_includes_shadow_doc_step(self):
        handoff_md = SKILLS_DIR / "qa" / "resources" / "checklist" / "16-handoff.md"
        content = handoff_md.read_text()
        step = content.split("### 16.3", 1)[1].split("### 16.4", 1)[0]
        assert '"strategy":"suggested"' in step
        assert '"shadows":"docs/team-standards.md"' in step
        assert "cmp -s docs/team-standards.md /tmp/team-standards.before" in step

    def test_handoff_includes_queued_startup_step(self):
        handoff_md = SKILLS_DIR / "qa" / "resources" / "checklist" / "16-handoff.md"
        content = handoff_md.read_text()
        step = content.split("### 16.4", 1)[1].split("\n---", 1)[0]
        code = step.split("```bash", 1)[1].split("```", 1)[0]
        assert "forge hook stop" in step
        assert "queued_handoff" in step
        assert "forge session list" in step
        assert "forge handoff run" not in code
