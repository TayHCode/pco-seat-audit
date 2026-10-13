# Contributing

Keep changes small and dependency-free where possible. Run the local test command from the repository root:

```sh
python3 -m unittest discover -s tests
```

Use synthetic examples only. Never add account data, credentials, personal data, or hosted workflow files. The API integration must remain optional and read-only.
