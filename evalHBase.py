import happybase
import time
import random
connection = happybase.Connection('10.1.94.57')
connection.tables()
tab = connection.table('aaron_memex_ht-images')
nb_test=5000
max_id=80000000
my_ids_int = random.sample(xrange(max_id), nb_test)
my_ids_str = [str(one_id) for one_id in my_ids_int]
print "Getting images:",my_ids_str
start_time = time.time()
#for key in my_ids:
#	tmp=tab.row(str(key))
#all_rows=tab.rows(my_ids_str,columns=('meta:ads_id',))
all_rows=tab.rows(my_ids_str)
comp_time = time.time()-start_time
print comp_time
