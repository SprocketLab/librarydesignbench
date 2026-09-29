import json

from more_itertools import chunked


print("CUTOVER_CHECK pyt:01_step mode=more_itertools")
print(json.dumps([list(group) for group in chunked([1, 2, 3, 4, 5], 2)]))
