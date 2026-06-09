---
name: mlops_training
description: ML model training, evaluation, and experiment tracking patterns
version: 1.0.0
tags: [mlops, python, machine-learning]
required_config: []
platform: []
---

# MLOps Training Skill

## Training Pipeline via python_execute
Write a Python script that:
1. Loads and preprocesses data
2. Trains model with tracked hyperparameters
3. Evaluates: accuracy, precision, recall, F1
4. Saves model to workspace/models/
5. Logs metrics to workspace/experiments.json

## Always Track
- Dataset size and split ratios
- All hyperparameters
- Metrics at each epoch/step
- Final model path and size
- Inference latency in milliseconds
