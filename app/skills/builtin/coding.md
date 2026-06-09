---
name: code_review
description: Systematic code review, refactoring, and quality improvement
version: 1.0.0
tags: [code, review, quality, refactor]
required_config: []
platform: []
---

# Code Review Skill

## Review Protocol
1. Read file with str_replace_editor
2. Identify: bugs, security issues, performance, style
3. For each issue: explain problem and provide fix
4. Apply fixes using str_replace_editor
5. Run code to verify fixes work
6. Write summary to workspace/code_review.md

## Quality Checks
- Input validation and error handling
- Edge cases and null/empty handling
- Performance bottlenecks (N+1 queries, nested loops)
- Security: injection, hardcoded secrets, path traversal
