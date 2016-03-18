# http://happybase.readthedocs.org/en/latest/user.html
import happybase,sys
#import numpy as np
import json
import time

connection = happybase.Connection('10.1.94.57')

# use fields: meta:columbia_near_dups, meta:columbia_near_dups_dist
tab_aaron = connection.table('aaron_memex_ht-images')

#use field: image:hash
tab_hash = connection.table('image_hash')

# use field: images:images_doc
tab_samples = connection.table('dig_isi_cdr2_ht_images_sample')


if __name__ == '__main__':

  for one_row in tab_samples.scan():
  	doc = one_row[1]['images:images_doc']
  	jd = json.loads(doc)
  	image_id=jd['crawl_data']['image_id']
  	print image_id
  	# TODO also get obj_parent, one_row[0] i.e. CDR_ID, crawl_data.memex_ht_id
  	hash_row = tab_hash.row(str(image_id))
  	sim_row = tab_aaron.row(str(image_id))
        if hash_row:
                print hash_row
	else:
		print "Hash row is empty. Skipping"
		continue # Get hash from MySQL or recompute from image
        if sim_row:
		print sim_row
	else:
		print "Sim row is empty. Skipping."
                continue # Should compute similarity from API?
  	print hash_row['image:hash']
        if 'meta:columbia_near_dups' in sim_row:
	  	print sim_row['meta:columbia_near_dups'], sim_row['meta:columbia_near_dups_dist']
  	else:
		print "Similarity not yet computed. Skipping"
		continue
	time.sleep(1)
