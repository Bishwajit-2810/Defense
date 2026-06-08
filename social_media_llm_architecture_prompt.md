# Social Media Analysis Platform - System Design Request

> **Note:** This is the original request. The authoritative, refined requirements
> now live in [what.txt](what.txt), and the design docs are written against that.
> This file has been reconciled with what.txt on the points that matter most:
> **no external/paid LLM API** (two local open-source LLMs only), **Banglish**
> (romanized Bangla) as a first-class language, and the **post + comment thread**
> as the unit of analysis.

## Project Overview

I want to design and implement a production-grade AI **microservice** ("smart
layer") capable of processing and analyzing large volumes of scraped Facebook and
Instagram content — **each post together with its comment thread**.

The system should process **1,000+ posts per batch** and eventually scale to
**10,000+ and then 100,000+ posts per batch** while remaining fast,
cost-effective, and horizontally scalable. A scraper feeds posts in; the smart
layer returns structured JSON out for downstream projects to consume.

---

# Goals

Build a multilingual AI-powered platform that:

- Ingests scraped Facebook and Instagram posts **with their comment threads**.
- Supports **Bangla, English, and Banglish** (romanized/code-mixed Bangla).
- Performs advanced AI/NLP analysis.
- Generates structured JSON output (summary in the post's original language).
- Runs the smart layer on **two local open-source LLMs — no external/paid API**.
- Supports batch and streaming workloads.
- Can be deployed on-premise or in the cloud.
- Uses load balancing and distributed processing.

---

# Functional Requirements

## Input Sources

- Facebook Posts **+ their comment threads**
- Instagram Captions
- Instagram Comments
- Mixed Bangla-English-**Banglish** Content
- Large Batch Uploads

### Target Scale

| Stage      | Posts    |
| ---------- | -------- |
| MVP        | 1,000+   |
| Production | 10,000+  |
| Future     | 100,000+ |

---

# AI Analysis Requirements

The system should support:

## Core Analysis

- Language Detection
- Sentiment Analysis
- Topic Classification
- Emotion Detection
- Intent Detection

## Entity Analysis

- Named Entity Recognition (NER)
- Person Detection
- Organization Detection
- Location Detection
- Brand Detection

## Content Safety

- Toxicity Detection
- Hate Speech Detection
- Offensive Content Detection

## Business Intelligence

- Trend Analysis
- Political Analysis
- Brand Mention Tracking
- Competitor Analysis

## LLM Features

- Post Summarization
- Cluster Summarization
- Insight Generation
- Report Generation

---

# Expected JSON Output

```json
{
  "post_id": "12345",
  "platform": "facebook",
  "language": "bn",
  "sentiment": "positive",
  "emotion": "joy",
  "topics": ["education", "technology"],
  "entities": [
    {
      "type": "organization",
      "value": "OpenAI"
    }
  ],
  "keywords": ["AI", "research"],
  "toxicity_score": 0.02,
  "summary": "Short summary",
  "confidence": 0.94,
  "created_at": "2026-06-01T10:00:00Z"
}
```

---

# Non-Functional Requirements

## Performance

- High throughput
- Low latency
- Parallel processing
- Batch processing support
- Distributed execution

## Cost Optimization

- **No external/paid LLM API** — two local open-source LLMs only (zero per-token cost)
- Prefer open-source models
- GPU optimization
- Smart routing of requests

## Scalability

- Horizontal scaling
- Auto-scaling
- Load balancing
- Queue-based architecture
- Fault tolerance

## Reliability

- Retry mechanisms
- Dead-letter queues
- Monitoring
- Logging
- Alerting

---

# Technical Background

Developer skills include:

- Python
- Node.js
- Flutter
- Linux
- AI/ML
- Deep Learning

---

# Architecture Design Request

Please provide a complete architecture including:

## System Design

- High-Level Architecture
- Component Diagram
- Data Flow Diagram
- Deployment Diagram

## Backend Services

- API Gateway
- Authentication Service
- Data Ingestion Service
- Analysis Service
- Reporting Service
- User Management Service

## AI Layer

Recommend:

- **Two local open-source LLMs** for Bangla + English + Banglish (no external API)
- Embedding Models
- Classification Models
- Fine-Tuning Strategy
- Model Serving Architecture

## Data Layer

Recommend:

- Operational Database
- Analytics Database
- Vector Database
- Caching Layer

Explain why each choice is appropriate.

---

# Load Balancing Requirements

Design a load balancing strategy covering:

- API Load Balancing
- Worker Load Balancing
- Queue Partitioning
- Horizontal Scaling

Explain:

- NGINX
- HAProxy
- Kubernetes Ingress
- Service Mesh Options

---

# Queue and Messaging Architecture

Recommend between:

- RabbitMQ
- Kafka
- Redis Streams
- NATS

Explain:

- Scalability
- Cost
- Reliability
- Operational Complexity

---

# Database Architecture

Recommend:

## Primary Database

Examples:

- MongoDB
- PostgreSQL

## Analytics Storage

Examples:

- ClickHouse
- Elasticsearch

## Vector Storage

Examples:

- Qdrant
- Weaviate
- Milvus

Provide tradeoffs.

---

# AI Model Selection

Recommend models for:

## Language Detection

## Sentiment Analysis

## Emotion Detection

## Toxicity Detection

## NER

## Summarization

## Insight Generation

Consider:

- Bangla Support
- English Support
- GPU Efficiency
- Open Source Availability

---

# RAG Evaluation

Determine:

- Whether RAG is needed.
- Benefits and drawbacks.
- Recommended vector database.
- Embedding model choices.

---

# GPU Requirements

Estimate infrastructure for:

## MVP

- 1,000 posts

## Production

- 10,000 posts

## Enterprise

- 100,000 posts

Recommend:

- Consumer GPUs
- Data Center GPUs
- Cloud Alternatives

Include cost estimates.

---

# Monitoring Stack

Recommend:

- Prometheus
- Grafana
- Loki
- OpenTelemetry
- Jaeger

Explain architecture.

---

# Caching Strategy

Recommend:

- Redis
- CDN
- Query Cache
- Embedding Cache
- LLM Response Cache

---

# API Design

Design REST APIs for:

## Ingestion

POST /posts/upload

## Batch Processing

POST /analysis/run

## Results

GET /analysis/{id}

## Reporting

GET /reports

Include request and response examples.

---

# Deployment Architecture

Compare:

## Docker Compose

Pros:

- Simplicity
- Low Cost

Cons:

- Limited Scalability

## Kubernetes

Pros:

- Auto Scaling
- High Availability

Cons:

- Complexity

Provide recommendations for MVP and Production.

---

# Cost Estimation

Estimate monthly costs for:

## MVP

1,000 Posts Per Batch

## Production

10,000 Posts Per Batch

## Large Scale

100,000 Posts Per Batch

Include:

- Compute
- Storage
- Networking
- LLM serving cost (local GPU compute — **no per-token API charges**)

---

# Additional Requirement

Do NOT recommend sending every post to an LLM.

Instead design a hybrid architecture where:

1. Small NLP models process most posts.
2. LLMs are used only when needed.
3. Token usage is minimized.
4. Costs remain low.

---

# Suggested Architecture Direction

```text
                    ┌────────────────────┐
                    │    API Gateway     │
                    └─────────┬──────────┘
                              │
                    ┌─────────▼──────────┐
                    │   Load Balancer    │
                    └─────────┬──────────┘
                              │
                    ┌─────────▼──────────┐
                    │ Ingestion Service  │
                    └─────────┬──────────┘
                              │
                    ┌─────────▼──────────┐
                    │ RabbitMQ / Kafka   │
                    └─────────┬──────────┘
                              │
         ┌────────────────────┼────────────────────┐
         │                    │                    │
 ┌───────▼───────┐   ┌────────▼────────┐   ┌──────▼───────┐
 │ NLP Worker 1  │   │ NLP Worker 2    │   │ NLP Worker N │
 └───────┬───────┘   └────────┬────────┘   └──────┬───────┘
         │                    │                   │
         └────────────────────┼───────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │ Classification &   │
                    │ Feature Extraction │
                    └─────────┬──────────┘
                              │
                    ┌──────────▼─────────────┐
                    │ Local LLM-A / LLM-B    │
                    │ (Selective · no API)   │
                    └──────────┬─────────────┘
                              │
                    ┌─────────▼──────────┐
                    │ JSON Generator     │
                    └─────────┬──────────┘
                              │
                    ┌─────────▼──────────┐
                    │ MongoDB / Qdrant   │
                    └─────────┬──────────┘
                              │
                    ┌─────────▼──────────┐
                    │ Analytics API      │
                    └─────────┬──────────┘
                              │
                    ┌─────────▼──────────┐
                    │ Flutter Dashboard  │
                    └────────────────────┘
```

---

# Deliverables Expected

Please provide:

1. High-Level Architecture
2. Low-Level Architecture
3. Service Breakdown
4. Technology Stack
5. Model Recommendations
6. Infrastructure Recommendations
7. Cost Estimates
8. Security Considerations
9. MVP Architecture
10. Production Architecture
11. Kubernetes Deployment Plan
12. Scaling Strategy
13. Performance Optimization Strategy
14. Fine-Tuning Strategy for Bangla and English Models
15. Best Practices for Processing 10,000+ Social Media Posts Efficiently
