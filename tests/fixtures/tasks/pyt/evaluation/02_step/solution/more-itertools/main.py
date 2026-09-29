import json

from more_itertools import flatten


print("CUTOVER_CHECK pyt:02_step mode=more_itertools")
print(json.dumps(list(flatten([[1, 2], [3], [4, 5]]))))
