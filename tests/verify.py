import hashlib, sys

def file_md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()

expected, filepath = sys.argv[1], sys.argv[2]
actual = file_md5(filepath)
if actual == expected:
    print("PASS")
    sys.exit(0)
else:
    print("FAIL: got " + actual)
    sys.exit(1)
