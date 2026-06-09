---
name: github_workflow
description: Automate GitHub operations - clone repos, PRs, issues, releases via API
version: 1.0.0
tags: [github, devops]
required_config: []
platform: []
---

# GitHub Workflow Skill

## Clone and setup a repo
Use bash tool to clone a repository and create a feature branch.

## Create PR via GitHub API
Use bash with curl and GITHUB_TOKEN to POST to /repos/OWNER/REPO/pulls with JSON body containing title, body, head branch, and base branch.

## List and manage issues
Use web_search or crawl to get issue data, or bash with curl to the GitHub API /repos/OWNER/REPO/issues endpoint.

## Best practices
- Always verify operation success by reading the API response
- Save results to workspace/
- Use GITHUB_TOKEN from environment
