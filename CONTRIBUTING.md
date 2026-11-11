# Contributing Guidelines

Thank you for your interest in contributing. Whether it is a bug report, new feature, correction, or
additional documentation, we welcome your input.

## Reporting bugs and requesting features

Use the GitHub issue tracker. Include a reproducible description, the stack parameters you used (without
secrets), the Region, and the relevant Lambda or Step Functions log excerpt.

## Contributing via pull requests

1. Work against the latest `main`.
2. Keep changes focused. Do not reformat unrelated code.
3. Run the checks locally:
   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r requirements-dev.txt
   PYTHONPATH=src:. pytest -q
   cfn-lint template.yaml
   ```
4. Describe the change and why in the pull request.

## Finding contributions to work on

Looking at the existing issues is a great way to find something to contribute on. As our projects, by default, use the default GitHub issue labels (enhancement/bug/duplicate/help wanted/invalid/question/wontfix), looking at any 'help wanted' issues is a great place to start.

## Code of Conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.

## Security issue notifications

If you discover a potential security issue in this project, notify AWS/Amazon Security via the
[vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Do not create a
public GitHub issue.

## Licensing

See the [LICENSE](LICENSE) file. We ask you to confirm the licensing of your contribution.
