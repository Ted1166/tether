# Tether

An AI collections agent that works inside the limits you set.

Tether helps freelancers and small businesses get paid. It turns messy messages into PayPal invoices, chases late payments, and negotiates payment plans, and every action it takes passes through a deterministic policy engine that you control.

The language model reads and words. Code decides.

## Status

Built so far: the policy engine, the payment plan generator, the state machines, the PayPal sandbox client, and the database layer with the invoice sync service.

## Run the tests

```
cd server
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## License

MIT
