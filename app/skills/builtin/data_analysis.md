---
name: data_analysis
description: Data loading, cleaning, analysis, and visualization pipeline
version: 1.0.0
tags: [data, analysis, pandas, visualization]
required_config: []
platform: []
---

# Data Analysis Skill

## Pipeline via python_execute
1. Load data (CSV, JSON, DB) with pandas
2. Explore: shape, dtypes, describe(), missing values
3. Clean: handle nulls, outliers, type conversion
4. Analyze: groupby, correlations, aggregations
5. Visualize: matplotlib charts saved to workspace/charts/
6. Write insights to workspace/analysis.md

## Key Checks
- Always check data shape and types first
- Report null/missing value counts
- Check for duplicates
- Validate value ranges make sense
