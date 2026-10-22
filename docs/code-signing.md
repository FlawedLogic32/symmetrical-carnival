# Releases and code signing

## Publishing a release

Push a tag that starts with `v` from `master`:

```bash
git tag v1.0.1
git push origin v1.0.1
```

CI runs the tests, builds `netmapper.exe` on Windows, signs it if signing is set up (below),
smoke tests the signed file, and publishes a GitHub release with:

- `netmapper-windows.zip`: the .exe plus README, example config and licence
- `netmapper.exe` on its own
- `SHA256SUMS.txt`: checksums for both

The release notes say whether the .exe is signed. Without signing the release is still
published, but SmartScreen will warn on first run.

Don't create the release by hand on the GitHub website: CI creates it, and fails if a
release for that tag already exists.

## Why sign

An unsigned .exe triggers SmartScreen's "Windows protected your PC" prompt and is more
likely to be flagged or blocked by endpoint security tools and application allow-listing.
A signature also proves the file came from you and was not changed. Note that a brand new
certificate still has to build SmartScreen reputation, so the first few downloads of a
signed build can still warn; the warning goes away as the file is used.

Since June 2023, public code-signing certificates must keep their private key in hardware
(a USB token or a cloud HSM). You can no longer buy a certificate as a plain `.pfx` file,
which is why the cloud option below is the easiest fit for CI.

## Option 1 (recommended): Azure Trusted Signing

Microsoft's managed signing service (also called Azure Artifact Signing). Around US$10 a
month for the Basic tier, no hardware token, and certificates are trusted by Windows and
SmartScreen. Microsoft validates your identity first: organisations need to be registered
for several years in a supported country, and individuals are supported in some countries.
Check the current eligibility on Microsoft's
[Trusted Signing page](https://learn.microsoft.com/azure/trusted-signing/) before you start.

1. In the Azure portal, create a **Trusted Signing account** and note its region endpoint,
   for example `https://eus.codesigning.azure.net/`.
2. Complete **identity validation** (organisation or individual). This can take a few days.
3. Create a **certificate profile** of type *Public Trust*.
4. Create an app registration (service principal) under **Microsoft Entra ID → App
   registrations**, and add a client secret.
5. On the Trusted Signing account, give that app the **Trusted Signing Certificate Profile
   Signer** role.
6. In GitHub, open **Settings → Secrets and variables → Actions** and add these
   repository secrets:

| Secret | Value |
| --- | --- |
| `AZURE_TENANT_ID` | Directory (tenant) ID of the app registration |
| `AZURE_CLIENT_ID` | Application (client) ID |
| `AZURE_CLIENT_SECRET` | The client secret's value |
| `TRUSTED_SIGNING_ENDPOINT` | The account's endpoint URL |
| `TRUSTED_SIGNING_ACCOUNT` | The Trusted Signing account name |
| `TRUSTED_SIGNING_PROFILE` | The certificate profile name |

## Option 2: a certificate in a `.pfx` file

Use this for a certificate you already hold as a `.pfx`, such as one issued by your
company's internal certificate authority (fine when the .exe only runs on company PCs that
trust that CA) or an older purchased certificate.

1. Convert the file to base64 on Windows:
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\path\to\cert.pfx")) | Set-Clipboard
   ```
2. Add these repository secrets:

| Secret | Value |
| --- | --- |
| `WINDOWS_CERT_PFX` | The base64 text from step 1 |
| `WINDOWS_CERT_PASSWORD` | The .pfx password |

The workflow writes the file to the runner's temp folder only for the signing step and
deletes it afterwards. It timestamps with `http://timestamp.digicert.com`; to use another
RFC 3161 server, add a repository **variable** (not secret) named `TIMESTAMP_URL`.

If both options are configured, Trusted Signing is used.

## Other options

Certificates from DigiCert, Sectigo, SSL.com and others now come on a USB token or in the
CA's cloud HSM (for example DigiCert KeyLocker or SSL.com eSigner). Those can be used from CI
with the vendor's own tool, but need a different signing step from the two above. Ask for
it to be added if you go that way.

## How signing behaves in CI

- Pull request builds are never signed, so untested code never gets your signature.
- Pushes to `master`, tags and manual runs are signed when the secrets exist.
- If secrets are set but the .exe comes out unsigned, the build fails.
- The **Check signature** step in the *Build netmapper.exe* job prints who signed the file.

To check a downloaded file on Windows, right-click it, choose **Properties → Digital
Signatures**, or run `Get-AuthenticodeSignature .\netmapper.exe`.
