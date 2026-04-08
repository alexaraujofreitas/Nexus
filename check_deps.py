# NOTE: exec() used here with hardcoded strings only — never with user input
checks = [
    ('PyTorch', 'import torch; print(torch.__version__)'),
    ('Transformers', 'import transformers; print(transformers.__version__)'),
    ('FinBERT cached', 'from transformers import pipeline; p=pipeline("text-classification", model="ProsusAI/finbert"); print(p(["Bitcoin surges to new high"])[0])'),
    ('Gymnasium', 'import gymnasium; print(gymnasium.__version__)'),
    ('ARCH/GARCH', 'from arch import arch_model; print("arch OK")'),
    ('hmmlearn', 'import hmmlearn; print(hmmlearn.__version__)'),
    ('VADER', 'from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer; print("VADER OK")'),
    ('feedparser', 'import feedparser; print("feedparser OK")'),
]

for name, code in checks:
    try:
        exec(code)
        print(f'  OK  {name}')
    except Exception as e:
        print(f'  FAIL {name}: {e}')