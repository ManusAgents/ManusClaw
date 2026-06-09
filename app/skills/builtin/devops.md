---
name: devops_deploy
description: Docker build, push, deploy patterns for common stacks
version: 1.0.0
tags: [devops, docker, deploy]
required_config: []
platform: [linux]
---

# DevOps Deployment Skill

## Docker Operations
Use bash tool to run docker commands:
- Build: docker build -t myapp:latest .
- Check status: docker ps --format table
- View logs: docker logs --tail 50 CONTAINER
- Stats: docker stats --no-stream CONTAINER

## Environment Verification
Always check service health after deploy using curl or browser_use.
Save deployment logs to workspace/deploy_TIMESTAMP.log

## Kubernetes
Use bash tool to apply manifests and check rollout status.
Always verify pod health after deployment.
