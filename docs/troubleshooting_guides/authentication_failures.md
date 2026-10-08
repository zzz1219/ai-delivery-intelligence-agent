# Authentication and SSO Failures

Related root cause: `authentication_failure`

## Typical symptoms
Users are redirected to login, get 401 errors, or tokens fail validation.

## Recommended diagnostic order
1. Check auth service logs for signature or mapping errors.
2. Verify token validity settings and signing keys.
3. Compare SSO attribute mapping with the identity provider.

## Common fixes
- Correct token settings and refresh keys.
- Fix the SSO attribute mapping.
