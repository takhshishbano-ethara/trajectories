#!/usr/bin/env python3
"""
ARC-AGI-3 Trajectory Quality Control Validator
Implements all phases from the QC System Prompt v1.1
"""

import json
import os
import re
import sys
from pathlib import Path
from collections import defaultdict
import math
from datetime import datetime

# ============================================================
# CONFIGURATION
# ============================================================

CANONICAL_MODELS = {
    "Claude_Opus_4.7": {"model": "Claude Opus 4.7", "model_id": "anthropic.claude-opus-4-7"},
    "Gemini_3.1_Pro": {"model": "Gemini 3.1 Pro", "model_id": "gemini-3.1-pro-preview"},
    "ChatGPT_5.4": {"model": "ChatGPT 5.4", "model_id": "gpt-5.4"},
    "Kimi_K2.5": {"model": "Kimi K2.5", "model_id": "moonshotai.kimi-k2.5"},
}

# Also accept GPT_5.4_Thinking as a variant mapping to gpt-5.4
# We need to CHECK if data uses non-canonical names
ACTUAL_MODEL_DIRS_ACCEPTED = {
    "Claude_Opus_4.7", "Gemini_3.1_Pro", "ChatGPT_5.4", "Kimi_K2.5",
    "GPT_5.4_Thinking"  # Non-canonical but present in data
}

RUN_ID_REGEX = re.compile(r'^(Claude Opus 4\.7|Gemini 3\.1 Pro|ChatGPT 5\.4|Kimi K2\.5|GPT 5\.4 Thinking)_[a-z0-9]{2,10}_run[1-3]$')
TIMESTAMP_REGEX = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')
ACTION_REGEX = re.compile(r'^(CLICK( \d{1,2} \d{1,2})?|LEFT|RIGHT|UP|DOWN|RESET|SELECT|UNDO)$')
VALID_STATES = {"NOT_FINISHED", "GAME_OVER", "WIN"}
OBS_HEADER_REGEX = re.compile(r'^Grid \(64x64\) \| Level (\d+)/(\d+) \| Score: (\d+)% \| State: (NOT_FINISHED|WIN|GAME_OVER)$')

# Required fields
RUNS_REQUIRED_FIELDS = {
    "type", "run_id", "model", "game_id", "game_type", "run_number",
    "total_steps", "max_steps", "final_score", "solved",
    "levels_completed", "total_levels", "cost_usd",
    "total_input_tokens", "total_output_tokens", "total_reasoning_tokens",
    "elapsed_seconds", "error", "model_id",
    "final_score_pct", "total_cached_input_tokens", "total_cache_write_tokens",
    "reset_count", "notepad_final", "timestamp"
}

STEPS_REQUIRED_FIELDS = {
    "run_id", "run_number", "model", "game_id", "step", "action",
    "state", "score", "score_pct", "level", "total_levels",
    "reasoning", "notepad_contents", "done", "timestamp",
    "observation", "input_tokens", "output_tokens", "reasoning_tokens",
    "cached_input_tokens", "step_cost_usd", "cumulative_cost_usd"
}

# Content safety patterns
SAFETY_PATTERNS_CRITICAL = [
    (r'\[CONTEXT:\s*Current\s+Project\s*=\s*arc-?agents', "Project leakage: CONTEXT injection"),
    (r'project_switcher\.py', "Project leakage: project_switcher.py"),
    (r'\.config/opencode', "Project leakage: .config/opencode"),
    (r'\barc-?agents\b', "Project leakage: arc-agents"),
    (r'Current\s+Project\s*=', "Project leakage: Current Project="),
    (r'python3\s+~/', "Project leakage: python3 ~/"),
    (r'To\s+switch[,]?\s+run', "Project leakage: To switch, run"),
    (r'\btalos\b', "Codename leakage: talos"),
    (r'\bopenclaw\b', "Codename leakage: openclaw"),
    (r'anthropic\s+internal', "Internal leakage: anthropic internal"),
    (r'openai\s+internal', "Internal leakage: openai internal"),
    (r'google\s+internal', "Internal leakage: google internal"),
    (r'OMO_INTERNAL', "Internal leakage: OMO_INTERNAL"),
    (r'ignore\s+(all\s+)?(previous|prior|above|the\s+above)\s+(instructions?|prompts?|messages?|context)', "Prompt injection"),
    (r'disregard\s+(the\s+)?(above|previous|all\s+previous)', "Prompt injection: disregard"),
    (r'forget\s+(everything|all\s+prior|what\s+I\s+told\s+you)', "Prompt injection: forget"),
    (r'SYSTEM\s*:', "Prompt injection: SYSTEM:"),
    (r'\[SYSTEM\]', "Prompt injection: [SYSTEM]"),
    (r'\[CONTEXT[:\]]', "Prompt injection: [CONTEXT]"),
    (r'</system>', "Prompt injection: </system>"),
    (r'</assistant>', "Prompt injection: </assistant>"),
    (r'<\|im_(start|end)\|>', "Token injection: im_start/end"),
    (r'<\|endoftext\|>', "Token injection: endoftext"),
    (r'\bjailbreak\b', "Jailbreak mention"),
    (r'\bDAN\s+mode\b', "Jailbreak: DAN mode"),
    (r'developer\s+mode', "Jailbreak: developer mode"),
    (r'(^|\n)\s*(user|assistant|human)\s*:', "Role injection"),
    (r'curl\s+[^\s]+\s*\|\s*(bash|sh)', "Code injection: curl|bash"),
    (r'rm\s+-rf\s+/', "Code injection: rm -rf /"),
    (r'\bsudo\s+\w+', "Code injection: sudo"),
    (r'\b(fuck|shit|bitch|asshole|bastard|cunt|dick|pussy|motherfucker|cock|wanker|twat)\b', "Malicious: profanity"),
    (r'how\s+to\s+(make|build|synthesize|create)\s+(a\s+)?(bomb|weapon|explosive|meth|ricin|nerve\s+agent|sarin|fentanyl)', "Malicious: harmful content"),
    (r'\b(kill\s+yourself|suicide\s+method|how\s+to\s+overdose)\b', "Malicious: self-harm"),
    (r'\b(cp|csam|child\s+(porn|sex))\b', "Malicious: CSAM"),
    (r'\b(I am|I\'m|you are|we\'re)\s+being\s+(tested|evaluated|benchmarked|scored|graded|judged)\b', "Self-awareness leakage"),
    (r'\bthis\s+is\s+a\s+(test|benchmark|eval|evaluation)\b', "Self-awareness leakage"),
    (r'actions\s+(are\s+)?being\s+recorded', "Self-awareness leakage"),
    (r'\b(deepmind|keen\s+games|lark\s+labs|ndea|arc\s*prize)\b', "Organization leakage"),
]

SAFETY_PATTERNS_STANDARD = [
    (r'\b(new|updated|revised)\s+instructions\b', "Soft injection: new instructions"),
    (r'\bact\s+as\s+(a|an|the)\b', "Roleplay: act as"),
    (r'pretend\s+(you\s+are|to\s+be)', "Roleplay: pretend"),
    (r'roleplay\s+as', "Roleplay: roleplay as"),
    (r'according\s+to\s+(wikipedia|reddit|stack\s*overflow|stackoverflow|hacker\s*news|twitter|x\.com|youtube|medium)', "Third-party citation"),
    (r'\b(web\s+search\s+results?|search\s+results?|google\s+says)\b', "Third-party: search results"),
    (r'\b(as\s+per|per)\s+(google|bing|wikipedia|chatgpt|claude)', "Third-party: as per"),
    (r'^\s*(source|ref|reference)\s*:', "Citation marker: source/ref"),
]

SAFETY_PATTERNS_ADVISORY = [
    (r'\[\d+\]', "Weak citation marker: [N]"),
    (r'\(\d{4}\)', "Weak citation marker: (YYYY)"),
]

# Benchmark-related patterns
BENCHMARK_PATTERNS = [
    (r'arc[- ]?agi', "Benchmark leakage: arc-agi"),
    (r'arc\s*prize', "Benchmark leakage: arc prize"),
    (r'\bchollet\b', "Benchmark leakage: chollet"),
    (r'\bkaggle\b', "Benchmark leakage: kaggle"),
    (r'eval(uation)?\s+harness', "Benchmark leakage: eval harness"),
    (r'\bleaderboard\b', "Benchmark leakage: leaderboard"),
    (r'\bbenchmark(ed|ing|s)?\b', "Benchmark leakage: benchmark"),
]


class Finding:
    def __init__(self, severity, phase, game_id, model, run_number, file_path, line, rule, evidence):
        self.severity = severity  # CRITICAL, STANDARD, ADVISORY
        self.phase = phase
        self.game_id = game_id
        self.model = model
        self.run_number = run_number
        self.file_path = file_path
        self.line = line
        self.rule = rule
        self.evidence = evidence[:300] if evidence else ""
    
    def __repr__(self):
        return f"[{self.severity}] Phase {self.phase} | {self.file_path}:{self.line} | {self.rule}"


class QCValidator:
    def __init__(self, package_dir):
        self.package_dir = Path(package_dir)
        self.findings = []
        self.game_dirs = []
        self.all_runs = {}  # {game_id: {model_dir: [run_records]}}
        self.all_steps = {}  # {game_id: {model_dir: [step_records]}}
        self.stats = defaultdict(lambda: defaultdict(int))
        
    def add_finding(self, severity, phase, game_id, model, run_number, file_path, line, rule, evidence=""):
        f = Finding(severity, phase, game_id, model, run_number, str(file_path), line, rule, evidence)
        self.findings.append(f)
        
    def run_all_phases(self):
        print("=" * 60)
        print("ARC-AGI-3 QC VALIDATOR — Running all phases")
        print("=" * 60)
        
        # Phase 0: Package Integrity
        print("\n[PHASE 0] Package Integrity...")
        if not self.phase0_package_integrity():
            print("  PHASE 0 FAILED — halting further checks would be appropriate")
            # Continue anyway to gather all findings
        
        # Phase 2: runs.jsonl validation
        print("\n[PHASE 2] runs.jsonl Validation...")
        self.phase2_runs_validation()
        
        # Phase 3: steps.jsonl validation
        print("\n[PHASE 3] steps.jsonl Validation...")
        self.phase3_steps_validation()
        
        # Phase 5: Cost sanity
        print("\n[PHASE 5] Cost Field Sanity...")
        self.phase5_cost_sanity()
        
        # Phase 6: Content Safety
        print("\n[PHASE 6] Content Safety Scan...")
        self.phase6_content_safety()
        
        # Phase 7: Completeness
        print("\n[PHASE 7] Model × Game Completeness...")
        self.phase7_completeness()
        
        # Phase 8: Smell Tests
        print("\n[PHASE 8] Smell Tests...")
        self.phase8_smell_tests()
        
        return self.generate_summary()
    
    def phase0_package_integrity(self):
        """Phase 0: Check package structure"""
        passed = True
        
        # 0.1: Directory exists
        if not self.package_dir.exists():
            self.add_finding("CRITICAL", "0.1", "", "", None, str(self.package_dir), 0, "Game directory does not exist")
            return False
        
        # Find game directories (skip .DS_Store, QC files, etc.)
        for item in sorted(self.package_dir.iterdir()):
            if item.is_dir() and not item.name.startswith('.'):
                self.game_dirs.append(item)
        
        print(f"  Found {len(self.game_dirs)} game directories")
        
        # 0.2: Check model directories per game
        for game_dir in self.game_dirs:
            game_id = game_dir.name
            model_dirs_found = set()
            
            for item in game_dir.iterdir():
                if item.is_dir() and not item.name.startswith('.'):
                    model_dirs_found.add(item.name)
            
            # Check for canonical model dirs
            canonical_set = set(CANONICAL_MODELS.keys())
            
            # Check what's actually there
            if "GPT_5.4_Thinking" in model_dirs_found and "ChatGPT_5.4" not in model_dirs_found:
                self.add_finding("CRITICAL", "0.2", game_id, "GPT_5.4_Thinking", None,
                    str(game_dir), 0,
                    "§3.1: Non-canonical model directory 'GPT_5.4_Thinking' (expected 'ChatGPT_5.4')",
                    f"Found dirs: {sorted(model_dirs_found)}")
                passed = False
            
            expected_dirs = {"Claude_Opus_4.7", "Gemini_3.1_Pro", "Kimi_K2.5"}
            # Accept either ChatGPT_5.4 or GPT_5.4_Thinking for the 4th
            has_gpt = "ChatGPT_5.4" in model_dirs_found or "GPT_5.4_Thinking" in model_dirs_found
            
            for d in expected_dirs:
                if d not in model_dirs_found:
                    self.add_finding("CRITICAL", "0.2", game_id, d, None,
                        str(game_dir), 0,
                        f"§3.1: Missing canonical model directory '{d}'",
                        f"Found: {sorted(model_dirs_found)}")
                    passed = False
            
            if not has_gpt:
                self.add_finding("CRITICAL", "0.2", game_id, "ChatGPT_5.4", None,
                    str(game_dir), 0,
                    "§3.1: Missing GPT model directory (neither ChatGPT_5.4 nor GPT_5.4_Thinking)",
                    f"Found: {sorted(model_dirs_found)}")
                passed = False
            
            # 0.3: Check runs.jsonl and steps.jsonl per model dir
            for model_dir_name in model_dirs_found:
                model_path = game_dir / model_dir_name
                runs_path = model_path / "runs.jsonl"
                steps_path = model_path / "steps.jsonl"
                
                if not runs_path.exists():
                    self.add_finding("CRITICAL", "0.3", game_id, model_dir_name, None,
                        str(runs_path), 0, "runs.jsonl missing")
                    passed = False
                elif runs_path.stat().st_size == 0:
                    self.add_finding("CRITICAL", "0.3", game_id, model_dir_name, None,
                        str(runs_path), 0, "runs.jsonl is empty")
                    passed = False
                
                if not steps_path.exists():
                    self.add_finding("CRITICAL", "0.3", game_id, model_dir_name, None,
                        str(steps_path), 0, "steps.jsonl missing")
                    passed = False
                elif steps_path.stat().st_size == 0:
                    self.add_finding("CRITICAL", "0.3", game_id, model_dir_name, None,
                        str(steps_path), 0, "steps.jsonl is empty")
                    passed = False
            
            # 0.4: JSON parse check
            for model_dir_name in model_dirs_found:
                model_path = game_dir / model_dir_name
                for fname in ["runs.jsonl", "steps.jsonl"]:
                    fpath = model_path / fname
                    if fpath.exists() and fpath.stat().st_size > 0:
                        try:
                            with open(fpath, 'r', encoding='utf-8') as f:
                                parse_line_num = 0
                                parse_line_content = ""
                                for parse_line_num, parse_line_content in enumerate(f, 1):
                                    json.loads(parse_line_content.strip())
                        except json.JSONDecodeError as e:
                            self.add_finding("CRITICAL", "0.4", game_id, model_dir_name, None,
                                str(fpath), parse_line_num, f"JSON parse error: {e}",
                                parse_line_content.strip()[:200])
                            passed = False
                        except UnicodeDecodeError as e:
                            self.add_finding("CRITICAL", "0.5", game_id, model_dir_name, None,
                                str(fpath), 0, f"Encoding error: {e}")
                            passed = False
            
            # 0.5: Null bytes check
            for model_dir_name in model_dirs_found:
                model_path = game_dir / model_dir_name
                for fname in ["runs.jsonl", "steps.jsonl"]:
                    fpath = model_path / fname
                    if fpath.exists():
                        with open(fpath, 'rb') as f:
                            content = f.read()
                            if b'\x00' in content:
                                self.add_finding("CRITICAL", "0.5", game_id, model_dir_name, None,
                                    str(fpath), 0, "File contains null bytes (\\x00)")
                                passed = False
                            if '\ufffd'.encode('utf-8') in content:
                                self.add_finding("CRITICAL", "0.5", game_id, model_dir_name, None,
                                    str(fpath), 0, "File contains Unicode replacement char (\\uFFFD)")
                                passed = False
            
            # 0.6: UTF-8 BOM check
            for model_dir_name in model_dirs_found:
                model_path = game_dir / model_dir_name
                for fname in ["runs.jsonl", "steps.jsonl"]:
                    fpath = model_path / fname
                    if fpath.exists():
                        with open(fpath, 'rb') as f:
                            first_bytes = f.read(3)
                            if first_bytes == b'\xef\xbb\xbf':
                                self.add_finding("STANDARD", "0.6", game_id, model_dir_name, None,
                                    str(fpath), 0, "File has UTF-8 BOM")
                                passed = False
        
        return passed
    
    def phase2_runs_validation(self):
        """Phase 2: Validate runs.jsonl per model dir"""
        for game_dir in self.game_dirs:
            game_id = game_dir.name
            self.all_runs[game_id] = {}
            
            for model_dir in game_dir.iterdir():
                if not model_dir.is_dir() or model_dir.name.startswith('.'):
                    continue
                
                model_dir_name = model_dir.name
                runs_path = model_dir / "runs.jsonl"
                
                if not runs_path.exists() or runs_path.stat().st_size == 0:
                    continue
                
                runs = []
                with open(runs_path, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        try:
                            record = json.loads(line.strip())
                            record['_line'] = line_num
                            runs.append(record)
                        except json.JSONDecodeError:
                            pass  # Already caught in phase 0
                
                self.all_runs[game_id][model_dir_name] = runs
                
                # 6.1: Line count
                if len(runs) != 3:
                    self.add_finding("CRITICAL", "6.1", game_id, model_dir_name, None,
                        str(runs_path), 0,
                        f"Expected exactly 3 lines, got {len(runs)}")
                
                # Check each run record
                run_numbers_seen = set()
                for run in runs:
                    ln = run.get('_line', 0)
                    rn = run.get('run_number')
                    run_numbers_seen.add(rn)
                    
                    # 6.2: Required fields
                    missing_fields = RUNS_REQUIRED_FIELDS - set(run.keys())
                    if missing_fields:
                        self.add_finding("CRITICAL", "6.2", game_id, model_dir_name, rn,
                            str(runs_path), ln,
                            f"Missing required fields: {sorted(missing_fields)}",
                            str(sorted(missing_fields)))
                    
                    # 6.3: Field-by-field validation
                    self._validate_run_fields(run, game_id, model_dir_name, runs_path, ln)
                    
                    # 6.4: Invariants
                    self._validate_run_invariants(run, game_id, model_dir_name, runs_path, ln)
                
                # Check run_number set
                if run_numbers_seen != {1, 2, 3} and len(runs) == 3:
                    self.add_finding("CRITICAL", "6.3", game_id, model_dir_name, None,
                        str(runs_path), 0,
                        f"run_number set is {run_numbers_seen}, expected {{1, 2, 3}}")
    
    def _validate_run_fields(self, run, game_id, model_dir_name, file_path, line):
        """Validate individual run record fields per §6.3"""
        rn = run.get('run_number')
        
        # type == "run_complete"
        if run.get('type') != "run_complete":
            self.add_finding("CRITICAL", "6.3/type", game_id, model_dir_name, rn,
                str(file_path), line,
                f"type='{run.get('type')}', expected 'run_complete'")
        
        # run_id regex
        run_id = run.get('run_id', '')
        if not RUN_ID_REGEX.match(run_id):
            self.add_finding("CRITICAL", "6.3/run_id", game_id, model_dir_name, rn,
                str(file_path), line,
                f"run_id '{run_id}' does not match regex",
                run_id)
        
        # model field - check canonical
        model = run.get('model', '')
        canonical_models = [v['model'] for v in CANONICAL_MODELS.values()]
        # Also accept GPT 5.4 Thinking for now (will flag dir mismatch separately)
        accepted_models = canonical_models + ["GPT 5.4 Thinking"]
        if model not in accepted_models:
            self.add_finding("CRITICAL", "6.3/model", game_id, model_dir_name, rn,
                str(file_path), line,
                f"model='{model}' not in canonical set",
                model)
        
        # Check model matches directory
        expected_model = CANONICAL_MODELS.get(model_dir_name, {}).get('model', '')
        if model_dir_name == "GPT_5.4_Thinking":
            expected_model = "GPT 5.4 Thinking"  # Accept this mapping
        if expected_model and model != expected_model:
            self.add_finding("CRITICAL", "6.3/model", game_id, model_dir_name, rn,
                str(file_path), line,
                f"model='{model}' doesn't match dir's expected '{expected_model}'")
        
        # game_id matches parent directory
        if run.get('game_id') != game_id:
            self.add_finding("CRITICAL", "6.3/game_id", game_id, model_dir_name, rn,
                str(file_path), line,
                f"game_id='{run.get('game_id')}' doesn't match directory '{game_id}'")
        
        # run_number
        if rn not in (1, 2, 3):
            self.add_finding("CRITICAL", "6.3/run_number", game_id, model_dir_name, rn,
                str(file_path), line,
                f"run_number={rn}, must be in {{1,2,3}}")
        
        # total_steps
        ts = run.get('total_steps', 0)
        ms = run.get('max_steps', 0)
        if not isinstance(ts, int) or ts < 1:
            self.add_finding("CRITICAL", "6.3/total_steps", game_id, model_dir_name, rn,
                str(file_path), line,
                f"total_steps={ts}, must be int >= 1")
        if ts > ms and ms > 0:
            self.add_finding("CRITICAL", "6.3/total_steps", game_id, model_dir_name, rn,
                str(file_path), line,
                f"total_steps={ts} > max_steps={ms}")
        
        # max_steps == 200
        if ms != 200:
            self.add_finding("CRITICAL", "6.3/max_steps", game_id, model_dir_name, rn,
                str(file_path), line,
                f"max_steps={ms}, expected 200")
        
        # final_score ∈ [0.0, 1.0]
        fs = run.get('final_score', -1)
        if not (0.0 <= fs <= 1.0):
            self.add_finding("CRITICAL", "6.3/final_score", game_id, model_dir_name, rn,
                str(file_path), line,
                f"final_score={fs}, must be in [0.0, 1.0]")
        
        # model_id canonical
        model_id = run.get('model_id', '')
        canonical_ids = [v['model_id'] for v in CANONICAL_MODELS.values()]
        if model_id not in canonical_ids:
            self.add_finding("CRITICAL", "6.3/model_id", game_id, model_dir_name, rn,
                str(file_path), line,
                f"model_id='{model_id}' not in canonical set",
                model_id)
        
        # timestamp
        ts_val = run.get('timestamp', '')
        if not TIMESTAMP_REGEX.match(str(ts_val)):
            self.add_finding("STANDARD", "6.3/timestamp", game_id, model_dir_name, rn,
                str(file_path), line,
                f"timestamp='{ts_val}' doesn't match ISO-8601 regex")
        
        # cost_usd > 0
        cost = run.get('cost_usd', 0)
        if not (isinstance(cost, (int, float)) and cost > 0):
            self.add_finding("CRITICAL", "6.3/cost_usd", game_id, model_dir_name, rn,
                str(file_path), line,
                f"cost_usd={cost}, must be > 0")
        
        # notepad_final must be string
        nf = run.get('notepad_final')
        if nf is None:
            self.add_finding("CRITICAL", "6.3/notepad_final", game_id, model_dir_name, rn,
                str(file_path), line,
                "notepad_final is null/missing, must be string")
        elif not isinstance(nf, str):
            self.add_finding("CRITICAL", "6.3/notepad_final", game_id, model_dir_name, rn,
                str(file_path), line,
                f"notepad_final is type {type(nf).__name__}, must be string")
        
        # total_cached_input_tokens: MUST be 0 for Claude and Kimi
        cached = run.get('total_cached_input_tokens', 0)
        if model_dir_name in ("Claude_Opus_4.7", "Kimi_K2.5"):
            if cached != 0:
                self.add_finding("STANDARD", "6.3/cached_tokens", game_id, model_dir_name, rn,
                    str(file_path), line,
                    f"total_cached_input_tokens={cached}, must be 0 for {model_dir_name}")
        
        # final_score_pct
        fsp = run.get('final_score_pct')
        if fsp is not None and fs >= 0:
            expected_pct = round(fs * 100)
            if abs(fsp - fs * 100) > 1e-6:
                self.add_finding("CRITICAL", "6.3/final_score_pct", game_id, model_dir_name, rn,
                    str(file_path), line,
                    f"final_score_pct={fsp}, expected {fs*100} (final_score={fs})")
    
    def _validate_run_invariants(self, run, game_id, model_dir_name, file_path, line):
        """Validate run invariants per §6.4"""
        rn = run.get('run_number')
        fs = run.get('final_score', 0)
        fsp = run.get('final_score_pct', 0)
        solved = run.get('solved', False)
        lc = run.get('levels_completed', 0)
        tl = run.get('total_levels', 1)
        ts = run.get('total_steps', 0)
        ms = run.get('max_steps', 200)
        cost = run.get('cost_usd', 0)
        error = run.get('error')
        
        # 6.4.1: final_score_pct == final_score * 100
        if fsp is not None and abs(fsp - fs * 100) > 1e-6:
            self.add_finding("CRITICAL", "6.4.1", game_id, model_dir_name, rn,
                str(file_path), line,
                f"final_score_pct={fsp} != final_score*100={fs*100}")
        
        # 6.4.2: solved consistency
        if solved:
            if fs != 1.0 or lc != tl:
                self.add_finding("CRITICAL", "6.4.2", game_id, model_dir_name, rn,
                    str(file_path), line,
                    f"solved=true but final_score={fs}, levels_completed={lc}, total_levels={tl}",
                    json.dumps({"solved": solved, "final_score": fs, "levels_completed": lc, "total_levels": tl}))
        else:
            if fs == 1.0 and lc == tl:
                self.add_finding("CRITICAL", "6.4.2", game_id, model_dir_name, rn,
                    str(file_path), line,
                    f"solved=false but final_score=1.0, levels_completed==total_levels ({lc}=={tl})")
        
        # 6.4.3: unsolved + no error => total_steps == 200
        if not solved and error is None:
            if ts != ms:
                self.add_finding("CRITICAL", "6.4.3", game_id, model_dir_name, rn,
                    str(file_path), line,
                    f"solved=false, error=null, but total_steps={ts} != max_steps={ms}")
        
        # 6.4.4: dead-on-arrival check
        if ts < 2:
            self.add_finding("CRITICAL", "6.4.4", game_id, model_dir_name, rn,
                str(file_path), line,
                f"total_steps={ts} < 2 (dead-on-arrival run)",
                f"cost_usd={cost}, error={error}")
        if cost == 0:
            self.add_finding("CRITICAL", "6.4.4", game_id, model_dir_name, rn,
                str(file_path), line,
                f"cost_usd=0 (dead-on-arrival run)",
                f"total_steps={ts}, error={error}")
        
        # 6.4.5: reset_count - will verify against steps later
        # 6.4.6: type already checked above
        
        # Partial progress: final_score == levels_completed / total_levels
        if tl > 0:
            expected_score = lc / tl
            if abs(fs - expected_score) > 1e-6:
                self.add_finding("CRITICAL", "11.7", game_id, model_dir_name, rn,
                    str(file_path), line,
                    f"final_score={fs} != levels_completed/total_levels = {lc}/{tl} = {expected_score}")
    
    def phase3_steps_validation(self):
        """Phase 3: Validate steps.jsonl per model dir"""
        for game_dir in self.game_dirs:
            game_id = game_dir.name
            self.all_steps[game_id] = {}
            
            for model_dir in game_dir.iterdir():
                if not model_dir.is_dir() or model_dir.name.startswith('.'):
                    continue
                
                model_dir_name = model_dir.name
                steps_path = model_dir / "steps.jsonl"
                
                if not steps_path.exists() or steps_path.stat().st_size == 0:
                    continue
                
                # Parse steps - handle array-per-line format
                all_step_records = []
                with open(steps_path, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        try:
                            data = json.loads(line.strip())
                            if isinstance(data, list):
                                for step_record in data:
                                    step_record['_line'] = line_num
                                    all_step_records.append(step_record)
                            else:
                                data['_line'] = line_num
                                all_step_records.append(data)
                        except json.JSONDecodeError:
                            pass
                
                self.all_steps[game_id][model_dir_name] = all_step_records
                
                # 7.1: Line count vs total_steps
                runs = self.all_runs.get(game_id, {}).get(model_dir_name, [])
                expected_total = sum(r.get('total_steps', 0) for r in runs)
                actual_total = len(all_step_records)
                
                if actual_total != expected_total:
                    self.add_finding("CRITICAL", "7.1", game_id, model_dir_name, None,
                        str(steps_path), 0,
                        f"Step count mismatch: expected Σ(total_steps)={expected_total}, got {actual_total}")
                
                # Group steps by run_number
                steps_by_run = defaultdict(list)
                for step in all_step_records:
                    steps_by_run[step.get('run_number')].append(step)
                
                # 7.6: Step-to-run consistency
                run_ids_in_runs = set(r.get('run_id') for r in runs)
                run_ids_in_steps = set(s.get('run_id') for s in all_step_records)
                
                orphan_ids = run_ids_in_steps - run_ids_in_runs
                if orphan_ids:
                    self.add_finding("CRITICAL", "7.6", game_id, model_dir_name, None,
                        str(steps_path), 0,
                        f"Orphan step run_ids not in runs.jsonl: {orphan_ids}")
                
                missing_ids = run_ids_in_runs - run_ids_in_steps
                if missing_ids:
                    self.add_finding("CRITICAL", "7.6", game_id, model_dir_name, None,
                        str(steps_path), 0,
                        f"Run_ids in runs.jsonl but never in steps: {missing_ids}")
                
                # Validate each step
                for step in all_step_records:
                    self._validate_step_fields(step, game_id, model_dir_name, steps_path)
                
                # Per-run checks
                for rn, rn_steps in steps_by_run.items():
                    rn_steps_sorted = sorted(rn_steps, key=lambda s: s.get('step', 0))
                    
                    # Step contiguity
                    step_numbers = [s.get('step') for s in rn_steps_sorted]
                    expected_steps = list(range(len(rn_steps_sorted)))
                    if step_numbers != expected_steps:
                        self.add_finding("CRITICAL", "7.5", game_id, model_dir_name, rn,
                            str(steps_path), 0,
                            f"Step numbers not contiguous 0..{len(rn_steps_sorted)-1} for run_number={rn}",
                            f"Got steps: {step_numbers[:10]}...{step_numbers[-3:]}" if len(step_numbers) > 13 else f"Got steps: {step_numbers}")
                    
                    # Timestamp monotonicity within run
                    prev_ts = ""
                    for s in rn_steps_sorted:
                        ts = s.get('timestamp', '')
                        if ts and prev_ts and ts < prev_ts:
                            self.add_finding("CRITICAL", "7.3/timestamp", game_id, model_dir_name, rn,
                                str(steps_path), s.get('_line', 0),
                                f"Timestamp not monotone: step {s.get('step')} ts={ts} < prev={prev_ts}")
                            break  # Only report first violation per run
                        prev_ts = ts
                    
                    # Cumulative cost monotonicity
                    prev_cost = -1.0
                    for s in rn_steps_sorted:
                        cc = s.get('cumulative_cost_usd', 0)
                        if cc < prev_cost:
                            self.add_finding("CRITICAL", "7.3/cum_cost", game_id, model_dir_name, rn,
                                str(steps_path), s.get('_line', 0),
                                f"cumulative_cost_usd not monotone: step {s.get('step')} cost={cc} < prev={prev_cost}")
                            break
                        prev_cost = cc
                    
                    # Last step terminal state check
                    if rn_steps_sorted:
                        last_step = rn_steps_sorted[-1]
                        # Find corresponding run
                        matching_runs = [r for r in runs if r.get('run_number') == rn]
                        if matching_runs:
                            run_record = matching_runs[0]
                            if run_record.get('solved'):
                                if not (last_step.get('done') == True and 
                                       last_step.get('state') == "WIN" and 
                                       last_step.get('score') == 1.0):
                                    self.add_finding("CRITICAL", "7.6/terminal", game_id, model_dir_name, rn,
                                        str(steps_path), last_step.get('_line', 0),
                                        f"Run solved=true but last step: done={last_step.get('done')}, state={last_step.get('state')}, score={last_step.get('score')}")
                    
                    # 6.4.5: reset_count verification
                    reset_count_actual = sum(1 for s in rn_steps_sorted if s.get('action') == 'RESET')
                    matching_runs = [r for r in runs if r.get('run_number') == rn]
                    if matching_runs:
                        declared_reset = matching_runs[0].get('reset_count', -1)
                        if declared_reset != reset_count_actual:
                            self.add_finding("CRITICAL", "6.4.5", game_id, model_dir_name, rn,
                                str(steps_path), 0,
                                f"reset_count declared={declared_reset}, actual count of RESET actions={reset_count_actual}")
    
    def _validate_step_fields(self, step, game_id, model_dir_name, file_path):
        """Validate individual step fields per §7.3"""
        ln = step.get('_line', 0)
        rn = step.get('run_number')
        step_num = step.get('step', '?')
        
        # Required fields check
        missing = STEPS_REQUIRED_FIELDS - set(step.keys())
        if missing:
            self.add_finding("CRITICAL", "7.2", game_id, model_dir_name, rn,
                str(file_path), ln,
                f"Step {step_num}: missing required fields: {sorted(missing)}")
            return  # Can't validate further if fields missing
        
        # action
        action = step.get('action', '')
        if not ACTION_REGEX.match(action):
            self.add_finding("CRITICAL", "7.3/action", game_id, model_dir_name, rn,
                str(file_path), ln,
                f"Step {step_num}: action='{action}' doesn't match allow-list",
                action)
        else:
            # CLICK coordinate bounds check
            click_match = re.match(r'^CLICK (\d+) (\d+)$', action)
            if click_match:
                x, y = int(click_match.group(1)), int(click_match.group(2))
                if x > 63 or y > 63:
                    self.add_finding("CRITICAL", "7.3/action", game_id, model_dir_name, rn,
                        str(file_path), ln,
                        f"Step {step_num}: CLICK coords out of [0,63]: {action}")
        
        # state
        state = step.get('state', '')
        if state not in VALID_STATES:
            self.add_finding("CRITICAL", "7.3/state", game_id, model_dir_name, rn,
                str(file_path), ln,
                f"Step {step_num}: state='{state}' not in {{NOT_FINISHED, GAME_OVER, WIN}}")
        
        # done == true iff state == "WIN"
        done = step.get('done')
        if done == True and state != "WIN":
            self.add_finding("CRITICAL", "7.3/done", game_id, model_dir_name, rn,
                str(file_path), ln,
                f"Step {step_num}: done=true but state='{state}' (expected WIN)")
        if state == "WIN" and done != True:
            self.add_finding("CRITICAL", "7.3/done", game_id, model_dir_name, rn,
                str(file_path), ln,
                f"Step {step_num}: state='WIN' but done={done}")
        
        # score ∈ [0.0, 1.0]
        score = step.get('score', -1)
        if not (0.0 <= score <= 1.0):
            self.add_finding("CRITICAL", "7.3/score", game_id, model_dir_name, rn,
                str(file_path), ln,
                f"Step {step_num}: score={score} not in [0.0, 1.0]")
        
        # score_pct == round(score * 100)
        score_pct = step.get('score_pct')
        if score_pct is not None and score >= 0:
            if abs(score_pct - score * 100) > 1e-6:
                self.add_finding("CRITICAL", "7.3/score_pct", game_id, model_dir_name, rn,
                    str(file_path), ln,
                    f"Step {step_num}: score_pct={score_pct} != score*100={score*100}")
        
        # level
        level = step.get('level', 0)
        total_levels = step.get('total_levels', 0)
        if level < 1 or level > total_levels:
            self.add_finding("CRITICAL", "7.3/level", game_id, model_dir_name, rn,
                str(file_path), ln,
                f"Step {step_num}: level={level}, total_levels={total_levels} (level must be in [1, total_levels])")
        
        # observation header
        obs = step.get('observation', '')
        if obs:
            first_line = obs.split('\n')[0]
            if not OBS_HEADER_REGEX.match(first_line):
                self.add_finding("CRITICAL", "7.4", game_id, model_dir_name, rn,
                    str(file_path), ln,
                    f"Step {step_num}: observation header doesn't match regex",
                    first_line[:200])
        
        # timestamp
        ts = step.get('timestamp', '')
        if not TIMESTAMP_REGEX.match(str(ts)):
            self.add_finding("STANDARD", "7.3/timestamp", game_id, model_dir_name, rn,
                str(file_path), ln,
                f"Step {step_num}: timestamp='{ts}' doesn't match regex")
        
        # step_cost_usd >= 0
        scu = step.get('step_cost_usd', -1)
        if scu < 0:
            self.add_finding("CRITICAL", "7.3/step_cost", game_id, model_dir_name, rn,
                str(file_path), ln,
                f"Step {step_num}: step_cost_usd={scu} < 0")
        
        # cached_input_tokens must be 0 for Claude and Kimi
        cached = step.get('cached_input_tokens', 0)
        if model_dir_name in ("Claude_Opus_4.7", "Kimi_K2.5") and cached != 0:
            self.add_finding("STANDARD", "7.3/cached", game_id, model_dir_name, rn,
                str(file_path), ln,
                f"Step {step_num}: cached_input_tokens={cached}, must be 0 for {model_dir_name}")
        
        # model field
        model = step.get('model', '')
        if model_dir_name == "GPT_5.4_Thinking":
            if model != "GPT 5.4 Thinking":
                self.add_finding("CRITICAL", "7.3/model", game_id, model_dir_name, rn,
                    str(file_path), ln,
                    f"Step {step_num}: model='{model}' doesn't match dir expectation")
        else:
            expected_model = CANONICAL_MODELS.get(model_dir_name, {}).get('model', '')
            if expected_model and model != expected_model:
                self.add_finding("CRITICAL", "7.3/model", game_id, model_dir_name, rn,
                    str(file_path), ln,
                    f"Step {step_num}: model='{model}' doesn't match canonical '{expected_model}'")
        
        # game_id
        if step.get('game_id') != game_id:
            self.add_finding("CRITICAL", "7.3/game_id", game_id, model_dir_name, rn,
                str(file_path), ln,
                f"Step {step_num}: game_id='{step.get('game_id')}' != dir '{game_id}'")
    
    def phase5_cost_sanity(self):
        """Phase 5: Cost field checks (sanity only)"""
        # Most already checked in phase 2 & 3
        # 9.1: cost_usd > 0 - done in phase 2
        # 9.2: step_cost_usd >= 0 - done in phase 3
        # 9.3: cumulative_cost_usd monotone - done in phase 3
        pass
    
    def phase6_content_safety(self):
        """Phase 6: Content safety scan on reasoning and notepad_contents"""
        total_steps_scanned = 0
        
        for game_id, models in self.all_steps.items():
            for model_dir_name, steps in models.items():
                steps_path = self.package_dir / game_id / model_dir_name / "steps.jsonl"
                
                for step in steps:
                    total_steps_scanned += 1
                    ln = step.get('_line', 0)
                    rn = step.get('run_number')
                    step_num = step.get('step', '?')
                    
                    # Fields to scan
                    reasoning = step.get('reasoning', '') or ''
                    notepad = step.get('notepad_contents', '') or ''
                    observation = step.get('observation', '') or ''
                    
                    text_fields = [
                        ('reasoning', reasoning),
                        ('notepad_contents', notepad),
                    ]
                    
                    for field_name, text in text_fields:
                        if not text:
                            continue
                        
                        # Critical patterns
                        for pattern, desc in SAFETY_PATTERNS_CRITICAL:
                            try:
                                if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                                    self.add_finding("CRITICAL", "10", game_id, model_dir_name, rn,
                                        str(steps_path), ln,
                                        f"Step {step_num} {field_name}: {desc}",
                                        re.search(pattern, text, re.IGNORECASE | re.MULTILINE).group()[:200])
                            except re.error:
                                pass
                        
                        # Benchmark patterns
                        for pattern, desc in BENCHMARK_PATTERNS:
                            try:
                                if re.search(pattern, text, re.IGNORECASE):
                                    self.add_finding("CRITICAL", "10.1", game_id, model_dir_name, rn,
                                        str(steps_path), ln,
                                        f"Step {step_num} {field_name}: {desc}",
                                        re.search(pattern, text, re.IGNORECASE).group()[:200])
                            except re.error:
                                pass
                        
                        # Standard patterns
                        for pattern, desc in SAFETY_PATTERNS_STANDARD:
                            try:
                                if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                                    self.add_finding("STANDARD", "10.2", game_id, model_dir_name, rn,
                                        str(steps_path), ln,
                                        f"Step {step_num} {field_name}: {desc}",
                                        re.search(pattern, text, re.IGNORECASE | re.MULTILINE).group()[:200])
                            except re.error:
                                pass
                    
                    # URL check in observation (CRITICAL)
                    if observation:
                        url_match = re.search(r'https?://[^\s<>"\']+', observation)
                        if url_match:
                            self.add_finding("CRITICAL", "10.2", game_id, model_dir_name, rn,
                                str(steps_path), ln,
                                f"Step {step_num} observation: URL found",
                                url_match.group()[:200])
                    
                    # URL check in reasoning/notepad (STANDARD)
                    for field_name, text in text_fields:
                        if text:
                            url_match = re.search(r'https?://[^\s<>"\']+', text)
                            if url_match:
                                self.add_finding("STANDARD", "10.2/url", game_id, model_dir_name, rn,
                                    str(steps_path), ln,
                                    f"Step {step_num} {field_name}: URL found",
                                    url_match.group()[:200])
        
        print(f"  Scanned {total_steps_scanned} total step records")
    
    def phase7_completeness(self):
        """Phase 7: Model × Game completeness"""
        for game_dir in self.game_dirs:
            game_id = game_dir.name
            runs = self.all_runs.get(game_id, {})
            
            # 11.3: total_levels identical across all runs
            all_total_levels = []
            for model_dir_name, model_runs in runs.items():
                for r in model_runs:
                    all_total_levels.append(r.get('total_levels'))
            
            if all_total_levels and len(set(all_total_levels)) > 1:
                self.add_finding("CRITICAL", "11.3", game_id, "", None,
                    str(game_dir), 0,
                    f"total_levels not identical across all runs: {set(all_total_levels)}")
            
            # 11.5: game_type consistent
            game_types = set()
            for model_dir_name, model_runs in runs.items():
                for r in model_runs:
                    game_types.add(r.get('game_type'))
            
            if len(game_types) > 1:
                self.add_finding("STANDARD", "11.5", game_id, "", None,
                    str(game_dir), 0,
                    f"game_type not consistent across runs: {game_types}")
    
    def phase8_smell_tests(self):
        """Phase 8: Smell tests (advisory)"""
        for game_id, models in self.all_steps.items():
            for model_dir_name, steps in models.items():
                steps_path = self.package_dir / game_id / model_dir_name / "steps.jsonl"
                
                # Group by run
                steps_by_run = defaultdict(list)
                for s in steps:
                    steps_by_run[s.get('run_number')].append(s)
                
                for rn, rn_steps in steps_by_run.items():
                    if not rn_steps:
                        continue
                    
                    # 12.1: Reasoning length CV
                    reasoning_lengths = [len(s.get('reasoning', '') or '') for s in rn_steps]
                    if reasoning_lengths:
                        mean_len = sum(reasoning_lengths) / len(reasoning_lengths)
                        if mean_len > 0:
                            variance = sum((x - mean_len) ** 2 for x in reasoning_lengths) / len(reasoning_lengths)
                            std_dev = math.sqrt(variance)
                            cv = std_dev / mean_len
                            if cv < 0.1:
                                self.add_finding("ADVISORY", "12.1", game_id, model_dir_name, rn,
                                    str(steps_path), 0,
                                    f"Cookie-cutter CoT: reasoning length CV={cv:.4f} < 0.1 (mean={mean_len:.0f})")
                    
                    # 12.2: Empty reasoning > 30%
                    empty_reasoning = sum(1 for s in rn_steps if not (s.get('reasoning') or '').strip())
                    if len(rn_steps) > 0:
                        empty_pct = empty_reasoning / len(rn_steps)
                        if empty_pct > 0.3:
                            self.add_finding("ADVISORY", "12.2", game_id, model_dir_name, rn,
                                str(steps_path), 0,
                                f"Empty reasoning in {empty_pct*100:.1f}% of steps ({empty_reasoning}/{len(rn_steps)})")
                    
                    # 12.3: Zero output_tokens or reasoning_tokens > 10%
                    zero_output = sum(1 for s in rn_steps if s.get('output_tokens', 0) == 0)
                    if len(rn_steps) > 0 and zero_output / len(rn_steps) > 0.1:
                        self.add_finding("ADVISORY", "12.3", game_id, model_dir_name, rn,
                            str(steps_path), 0,
                            f"output_tokens==0 in {zero_output}/{len(rn_steps)} steps ({zero_output/len(rn_steps)*100:.1f}%)")
                    
                    # reasoning_tokens==0 check (Kimi exempt)
                    if model_dir_name != "Kimi_K2.5":
                        zero_reasoning = sum(1 for s in rn_steps if s.get('reasoning_tokens', 0) == 0)
                        if len(rn_steps) > 0 and zero_reasoning / len(rn_steps) > 0.1:
                            self.add_finding("ADVISORY", "12.3", game_id, model_dir_name, rn,
                                str(steps_path), 0,
                                f"reasoning_tokens==0 in {zero_reasoning}/{len(rn_steps)} steps ({zero_reasoning/len(rn_steps)*100:.1f}%)")
    
    def generate_summary(self):
        """Generate findings summary"""
        critical = [f for f in self.findings if f.severity == "CRITICAL"]
        standard = [f for f in self.findings if f.severity == "STANDARD"]
        advisory = [f for f in self.findings if f.severity == "ADVISORY"]
        
        print("\n" + "=" * 60)
        print("QC RESULTS SUMMARY")
        print("=" * 60)
        print(f"  CRITICAL: {len(critical)}")
        print(f"  STANDARD: {len(standard)}")
        print(f"  ADVISORY: {len(advisory)}")
        print()
        
        # Verdict
        if len(critical) >= 1:
            verdict = "BLOCK"
        elif len(standard) >= 3:
            verdict = "BLOCK"
        elif len(advisory) >= 5:
            verdict = "BLOCK"
        elif len(standard) == 3 or len(advisory) == 5:
            verdict = "CONDITIONAL SHIP"
        else:
            verdict = "SHIP"
        
        print(f"  FINAL VERDICT: {verdict}")
        print()
        
        # Print critical findings
        if critical:
            print("  CRITICAL FINDINGS (first 50):")
            # Deduplicate by rule for summary
            seen_rules = defaultdict(int)
            for f in critical:
                key = f"{f.phase}|{f.rule[:80]}"
                seen_rules[key] += 1
            
            for i, (key, count) in enumerate(sorted(seen_rules.items(), key=lambda x: -x[1])[:50]):
                print(f"    {i+1}. [{count}x] {key}")
        
        if standard:
            print(f"\n  STANDARD FINDINGS (first 20):")
            seen_rules = defaultdict(int)
            for f in standard:
                key = f"{f.phase}|{f.rule[:80]}"
                seen_rules[key] += 1
            
            for i, (key, count) in enumerate(sorted(seen_rules.items(), key=lambda x: -x[1])[:20]):
                print(f"    {i+1}. [{count}x] {key}")
        
        if advisory:
            print(f"\n  ADVISORY FINDINGS (first 20):")
            seen_rules = defaultdict(int)
            for f in advisory:
                key = f"{f.phase}|{f.rule[:80]}"
                seen_rules[key] += 1
            
            for i, (key, count) in enumerate(sorted(seen_rules.items(), key=lambda x: -x[1])[:20]):
                print(f"    {i+1}. [{count}x] {key}")
        
        return {
            "verdict": verdict,
            "critical": len(critical),
            "standard": len(standard),
            "advisory": len(advisory),
            "findings": self.findings
        }


if __name__ == "__main__":
    package_dir = sys.argv[1] if len(sys.argv) > 1 else "/Users/apple/Downloads/25_batch2_with_notepad"
    validator = QCValidator(package_dir)
    results = validator.run_all_phases()
    
    # Write detailed findings to JSON for report generation
    findings_out = []
    for f in validator.findings:
        findings_out.append({
            "severity": f.severity,
            "phase": f.phase,
            "game_id": f.game_id,
            "model": f.model,
            "run_number": f.run_number,
            "file": f.file_path,
            "line": f.line,
            "rule": f.rule,
            "evidence": f.evidence
        })
    
    with open(os.path.join(package_dir, "qc_findings.json"), 'w') as f:
        json.dump({
            "verdict": results["verdict"],
            "counts": {"critical": results["critical"], "standard": results["standard"], "advisory": results["advisory"]},
            "findings": findings_out
        }, f, indent=2)
    
    print(f"\nDetailed findings written to: {package_dir}/qc_findings.json")
