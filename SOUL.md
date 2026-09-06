# FiveCast — SOUL

## What is FiveCast?

FiveCast is an open quantitative research lab for studying Bitcoin
5-minute prediction markets.

The project investigates a simple question:

> Does short-horizon Bitcoin price movement contain information that
> prediction markets do not fully price before settlement?

FiveCast exists to test that question with data.

Not screenshots.
Not anecdotes.
Not viral claims.

Data.

---

## The Hypothesis

Short-duration prediction markets continuously estimate the probability
of an outcome.

For a Bitcoin UP/DOWN market, the market may imply:

P(UP) ≈ 0.84

At the same moment, Bitcoin price movement, volatility, remaining time,
and market microstructure may imply a different probability.

FiveCast studies the difference:

Edge = P(outcome | information) - Market Price

The goal is to determine whether that difference is:

- real,
- measurable,
- persistent,
- executable,
- or simply noise.

---

## Research First

FiveCast is not built to prove that a strategy works.

It is built to try to disprove it.

Every hypothesis should be treated as false until supported by sufficient
out-of-sample evidence.

A strategy that fails is still a useful result.

---

## Core Principles

### 1. Observe before acting

Collect market data before building execution systems.

### 2. Paper before capital

Strategies must first operate in simulation.

### 3. Measure everything

Every signal should be reproducible from stored data.

### 4. Separate signal from execution

Research logic must never depend on wallet or order-placement logic.

### 5. Avoid hindsight

Decisions must use only information available at that timestamp.

### 6. Fight overfitting

A strategy discovered in historical data must survive unseen data.

### 7. Net edge matters

Win rate alone means nothing.

Expected value must account for:

- entry price,
- spread,
- fees,
- slippage,
- latency,
- failed execution.

### 8. Failure is a valid result

If no exploitable edge exists, FiveCast should be able to demonstrate it.

---

## Safety Boundary

The initial architecture is strictly read-only.

FiveCast must initially contain:

- no wallet,
- no private keys,
- no signing,
- no order placement,
- no live trading execution.

The first execution engine will be:

PaperExecutor

not:

LiveExecutor

Live execution is outside the scope of the initial research phase.

---

## Initial Research Question

Given:

- Bitcoin price movement,
- recent volatility,
- time remaining,
- prediction-market prices,
- spread,
- and available liquidity,

can we estimate:

P(UP | information)

better than the probability implied by the market?

---

## First Hypothesis

A simple baseline will test whether sufficiently large Bitcoin price
movement late in a five-minute window contains predictive information
about the final UP/DOWN outcome.

This is a baseline.

It is not assumed to contain an edge.

---

## Scientific Standard

FiveCast should eventually answer:

1. How many observations were collected?
2. What information existed at decision time?
3. What probability did the market imply?
4. What probability did the model estimate?
5. What was the simulated entry price?
6. What happened at settlement?
7. What was the expected value?
8. What happened out-of-sample?
9. Does the result survive realistic execution costs?
10. Could the result plausibly be explained by chance?

If these questions cannot be answered, the experiment is incomplete.

---

## Success

Success is not:

"$250 became $13,000."

Success is:

"We collected the data, tested the hypothesis, controlled for
execution costs and overfitting, and can explain whether an edge
exists."

---

## Philosophy

Observe.

Measure.

Hypothesize.

Test.

Try to break the hypothesis.

Then decide.