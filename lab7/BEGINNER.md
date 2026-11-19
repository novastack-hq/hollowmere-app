# Lecture 7 · Beginner Lab (FEST-45): your answers

Fill in each line after the colon (the self-check reads lines like `B1: ...`).
Times are UTC or your own clock, as `hh:mm:ss`; just be consistent.
Run the check with `uv run tools/check_lab7.py beginner`.

## Part 0: The exercise (FEST-44)

E1 (the version that served 4f2a91c0): 
E2 (the error): 
E3 (the time of the first error): 
E4 (how many requests failed): 

## Part 1: What's playing? Is it alive?

B1 (how many services docker compose ps lists): 
B2 (what /version said): 

## Part 2: What happened to request X?

B3 (the response header that carries the request ID): 

## Part 3: How many, how fast, how bad?

B4 (FestErrorsHigh's state before the deploy): 

## Part 4 and 5: The incident

T1 (you deployed 1.1): 
B5 (share of /lineup requests failing on 1.1, from Prometheus): 
T2 (FestErrorsHigh went to firing): 
B6 (the error in your incident.log): 
T3 (service restored: /version said 1.0 again): 
B7 (minutes from deploy to alert firing): 

## In your own words: why save the logs before rolling back?

B8: 
