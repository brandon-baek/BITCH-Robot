"""A home-made certificate so your phone can use BITCH securely (https) on your Wi-Fi.

Phones allow the microphone and camera on secure pages. BITCH makes its own little
certificate authority ("BITCH Home") once; you install it on your phone one time (the app shows how), and from
then on https://<your Mac>.local:8781 is trusted. Everything stays on your own network.
"""
import datetime as dt, ipaddress, os, socket, subprocess
from . import config

DIR = os.path.join(config.HOME, 'tls')
CA_KEY, CA_CRT, CA_DER = (os.path.join(DIR, f) for f in ('ca.key', 'ca.pem', 'BITCH-Home.crt'))
KEY, CRT, NAMES = (os.path.join(DIR, f) for f in ('server.key', 'server.pem', 'names.txt'))


def host():
    try: return subprocess.run(['scutil', '--get', 'LocalHostName'], capture_output=True, text=True, timeout=3).stdout.strip() + '.local'
    except Exception: return socket.gethostname()


def ips():
    out = {'127.0.0.1'}
    for i in ('en0', 'en1', 'en2'):
        try:
            a = subprocess.run(['ipconfig', 'getifaddr', i], capture_output=True, text=True, timeout=2).stdout.strip()
            if a: out.add(a)
        except Exception: pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('10.255.255.255', 1)); out.add(s.getsockname()[0]); s.close()
    except Exception: pass
    return sorted(out)


def ensure():
    """Make (or refresh) the certificates. Returns (cert file, key file)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    os.makedirs(DIR, exist_ok=True); os.chmod(DIR, 0o700)
    now = dt.datetime.now(dt.timezone.utc)
    if not (os.path.exists(CA_KEY) and os.path.exists(CA_CRT)):
        k = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'BITCH Home'), x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'BITCH desk robot')])
        c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key()).serial_number(x509.random_serial_number())
             .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
             .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
             .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
             .add_extension(x509.SubjectKeyIdentifier.from_public_key(k.public_key()), False)
             .sign(k, hashes.SHA256()))
        open(CA_KEY, 'wb').write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        os.chmod(CA_KEY, 0o600)
        open(CA_CRT, 'wb').write(c.public_bytes(serialization.Encoding.PEM))
        open(CA_DER, 'wb').write(c.public_bytes(serialization.Encoding.DER))
    names = sorted({host(), 'localhost'}) + ips()
    want = ' '.join(names)
    fresh = os.path.exists(CRT) and os.path.exists(NAMES) and open(NAMES).read() == want
    if fresh:
        c = x509.load_pem_x509_certificate(open(CRT, 'rb').read())
        fresh = c.not_valid_after_utc - now > dt.timedelta(days=30)
    if not fresh:
        ca_k = serialization.load_pem_private_key(open(CA_KEY, 'rb').read(), None)
        ca_c = x509.load_pem_x509_certificate(open(CA_CRT, 'rb').read())
        k = ec.generate_private_key(ec.SECP256R1())
        san = [x509.DNSName(n) for n in names if not n[0].isdigit()] + [x509.IPAddress(ipaddress.ip_address(n)) for n in names if n[0].isdigit()]
        c = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host())])).issuer_name(ca_c.subject)
             .public_key(k.public_key()).serial_number(x509.random_serial_number())
             .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=800))     # phones refuse longer
             .add_extension(x509.SubjectAlternativeName(san), False)
             .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
             .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
             .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), True)
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_k.public_key()), False)
             .sign(ca_k, hashes.SHA256()))
        open(KEY, 'wb').write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        os.chmod(KEY, 0o600)
        open(CRT, 'wb').write(c.public_bytes(serialization.Encoding.PEM) + open(CA_CRT, 'rb').read())
        open(NAMES, 'w').write(want)
    return CRT, KEY
