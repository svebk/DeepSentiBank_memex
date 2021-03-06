import sys
print(sys.version)

import json
import time
import datetime
import subprocess
from argparse import ArgumentParser
from pyspark import SparkContext, SparkConf, StorageLevel
from elastic_manager import ES
from hbase_manager import HbaseManager

#dev = True
dev = False

# Some parameters
default_identifier = None
default_batch_update_size = 10000
max_ts = 9999999999999
max_ads_image_dig = 20000
max_ads_image_hbase = 20000
max_ads_image = 20000
#max_samples_per_partition = 10000
max_samples_per_partition = 20000
default_partitions_nb = 64
day_gap = 86400000 # One day
valid_url_start = 'https://s3'
compression = "org.apache.hadoop.io.compress.GzipCodec"
#compression = None

fields_cdr = ["obj_stored_url", "obj_parent", "content_type"]
fields_list = [("info","s3_url"), ("info","all_parent_ids"), ("info","image_discarded"), ("info","cu_feat_id"), ("info","img_info")]

# the base_hdfs_path could be set with a parameter too
if dev:
    dev_release_suffix = "_dev"
    base_hdfs_path = '/user/skaraman/data/test_get_images_v2/'
    #base_hdfs_path = "/Users/svebor/Documents/Workspace/CodeColumbia/MEMEX/tmpdata/"
else:
    dev_release_suffix = ""
    base_hdfs_path = '/user/worker/dig2/incremental/'

##-- Hbase (happybase)

def get_create_table(table_name, options, families={'info': dict()}):
    try:
        from happybase.connection import Connection
        conn = Connection(options.hbase_ip)
        try:
            table = conn.table(table_name)
            # this would fail if table does not exist
            fam = table.families()
            return table
        # what exception would be raised if table does not exist, actually none.
        # need to try to access families to get error
        except Exception as inst:
            print "[get_create_table: info] table {} does not exist (yet)".format(table_name)
            conn.create_table(table_name, families)
            table = conn.table(table_name)
            print "[get_create_table: info] created table {}".format(table_name)
            return table
    except Exception as inst:
        print inst

##-- General RDD I/O
##------------------

def get_partitions_nb(options, rdd_count=0):
    """ Calculate number of partitions for a RDD.
    """
    # if options.nb_partitions is set use that
    if options.nb_partitions > 0:
        partitions_nb = options.nb_partitions
    elif rdd_count > 0: # if options.nb_partitions is -1 (default)
        #estimate from rdd_count and options.max_samples_per_partition
        import numpy as np
        partitions_nb = int(np.ceil(float(rdd_count)/options.max_samples_per_partition))
    else: # fall back to default partitions nb
        partitions_nb = default_partitions_nb
    print "[get_partitions_nb: log] partitions_nb: {}".format(partitions_nb)
    return partitions_nb


def get_list_value(json_x,field_tuple):
    return [x["value"] for x in json_x if x["columnFamily"]==field_tuple[0] and x["qualifier"]==field_tuple[1]]


def check_hdfs_file(hdfs_file_path):
    proc = subprocess.Popen(["hdfs", "dfs", "-ls", hdfs_file_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate()
    if "Filesystem closed" in err:
        print("[check_hdfs_file: WARNING] Beware got error '{}' when checking for file: {}.".format(err, hdfs_file_path))
        sys.stdout.flush()
    print "[check_hdfs_file] out: {}, err: {}".format(out, err)
    return out, err


def hdfs_file_exist(hdfs_file_path):
    out, err = check_hdfs_file(hdfs_file_path)
    # too restrictive as even log4j error would be interpreted as non existing file
    #hdfs_file_exist = "_SUCCESS" in out and not "_temporary" in out and not err
    hdfs_file_exist = "_SUCCESS" in out
    return hdfs_file_exist


def hdfs_file_failed(hdfs_file_path):
    out, err = check_hdfs_file(hdfs_file_path)
    hdfs_file_failed = "_temporary" in out
    return hdfs_file_failed


def load_rdd_json(basepath_save, rdd_name):
    rdd_path = basepath_save + "/" + rdd_name
    rdd = None
    try:
        if hdfs_file_exist(rdd_path):
            print("[load_rdd_json] trying to load rdd from {}.".format(rdd_path))
            rdd = sc.sequenceFile(rdd_path).mapValues(json.loads)
    except Exception as inst:
        print("[load_rdd_json: caught error] could not load rdd from {}. Error was {}.".format(rdd_path, inst))
    return rdd


def save_rdd_json(basepath_save, rdd_name, rdd, incr_update_id, hbase_man_update_out):
    rdd_path = basepath_save + "/" + rdd_name
    if not rdd.isEmpty():
        try:
            if not hdfs_file_exist(rdd_path):
                print("[save_rdd_json] saving rdd to {}.".format(rdd_path))
                #rdd.mapValues(json.dumps).saveAsSequenceFile(rdd_path)
                rdd.mapValues(json.dumps).saveAsSequenceFile(rdd_path, compressionCodecClass=compression)
            else:
                print("[save_rdd_json] skipped saving rdd to {}. File already exists.".format(rdd_path))
            save_info_incremental_update(hbase_man_update_out, incr_update_id, rdd_path, rdd_name+"_path")
        except Exception as inst:
            print("[save_rdd_json: caught error] could not save rdd at {}, error was {}.".format(rdd_path, inst))
    else:
        save_info_incremental_update(hbase_man_update_out, incr_update_id, "EMPTY", rdd_name+"_path")


def save_info_incremental_update(hbase_man_update_out, incr_update_id, info_value, info_name):
    print("[save_info_incremental_update] saving update info {}: {}".format(info_name, info_value))
    incr_update_infos_list = []
    incr_update_infos_list.append((incr_update_id, [incr_update_id, "info", info_name, str(info_value)]))
    incr_update_infos_rdd = sc.parallelize(incr_update_infos_list)
    hbase_man_update_out.rdd2hbase(incr_update_infos_rdd)

##------------------
##-- END General RDD I/O


##-- S3 URL functions
##-------------------

#- only if we use joins
def clean_up_s3url_sha1(data):
    try:
        s3url = unicode(data[0]).strip()
        json_x = [json.loads(x) for x in data[1].split("\n")]
        sha1 = get_list_value(json_x,("info","sha1"))[0].strip()
        return [(s3url, sha1)]
    except:
        print("[clean_up_s3url_sha1] failed, data was: {}".format(data))
        return []
#-

def get_SHA1_imginfo_from_URL(URL,verbose=0):
    import image_dl
    import json
    sha1hash,img_info = image_dl.get_SHA1_imginfo_from_URL_StringIO(URL, verbose) # 1 is verbose level
    return sha1hash, json.dumps(img_info)


def check_get_sha1_imginfo_s3url(data):
    URL_S3 = data[0]
    row_sha1, img_info = get_SHA1_imginfo_from_URL(URL_S3, 1)
    if row_sha1:
        out = [(URL_S3, (list([data[1][0]]), row_sha1, img_info))]
        #print out
        return out
    return []


def reduce_s3url_listadid(a, b):
    """ Reduce to get unique s3url with list of corresponding ad ids.
    """
    a.extend(b)
    return a


def s3url_listadid_sha1_imginfo_to_sha1_alldict(data):
    """ Transforms data expected to be in format (s3_url, ([ad_id], sha1, imginfo)) into a list 
    of tuples (sha1, v) where v contains the "info:s3_url", "info:all_parent_ids" and "info:img_info".
    """
    if len(data[1]) != 3 or data[1][1] is None or data[1][1] == 'None' or data[1][1] == u'None':
        print("[s3url_listadid_imginfo_to_sha1_alldict] incorrect data: {}".format(data))
        return []
    s3_url = data[0]
    listadid = list(data[1][0])
    sha1 = data[1][1]
    img_info = data[1][2]
    all_parent_ids = []
    # if we have a valid sha1
    if sha1:
        # add each ad_id containing this s3_url to all_parent_ids
        for ad_id in listadid: # could this split an ad_id into charachters?
            if len(ad_id)>1:
                all_parent_ids.append(ad_id)
    if sha1 and s3_url and all_parent_ids and img_info:
        out = [(sha1, {"info:s3_url": [s3_url], "info:all_parent_ids": all_parent_ids, "info:img_info": [img_info]})]
        #print out
        return out
    return []



##-------------------
##-- END S3 URL functions

###-------------
### Transformers

# function naming convention is input_to_output
# input/output can indicate key_value if relevant

def CDRv3_to_s3url_adid(data):
    """ Create tuples (s3_url, ad_id) for documents in CDRv3 format.

    :param data: CDR v3 ad document in JSON format
    """
    # TODO: check this is correct comparing to full-pipeline workflow.
    tup_list = []
    ad_id = data[0]
    # parse JSON
    json_x = json.loads(data[1])
    # look for images in objects field
    for obj in json_x["objects"]:
        # check that content_type corresponds to an image
        if obj["content_type"][0].startswith("image/"):
            # get url, some url may need unicode characters
            s3_url = unicode(obj["obj_stored_url"])
            if s3_url.startswith('https://s3'):
                tup_list.append( (s3_url, ad_id) )
    return tup_list

def CDRv2_to_s3url_adid(data):
    """ Create tuples (s3_url, ad_id) for documents in CDRv2 format.

    :param data: CDR v2 image document in JSON format
    """
    tup_list = []
    # parse JSON
    json_x = json.loads(data[1])
    #print json_x
    if json_x["content_type"][0].startswith("image/"):
        # get url, some url may need unicode characters
        s3_url = unicode(json_x["obj_stored_url"][0])
        ad_id = str(json_x["obj_parent"][0])
        if s3_url.startswith('https://s3'):
            tup_list.append( (s3_url, ad_id) )
    else:
        print "[CDRv2_to_s3url_adid: warning] {} not an image document!".format(data[0])
    return tup_list


def sha1_key_json_values(data):
    # when data was read from HBase and called with flatMapValues
    json_x = [json.loads(x) for x in data.split("\n")]
    v = dict()
    for field in fields_list:
        try:
            # if field is a list of ids
            if field[1]!='s3_url' and field[1]!='img_info': 
                v[':'.join(field)] = list(set([x for x in get_list_value(json_x,field)[0].strip().split(',')]))
            else: # s3url or img_info
                value = [unicode(get_list_value(json_x,field)[0].strip())]
                if field[1]=='img_info':
                    # discard value from HBase to avoid any reduce issue.
                    value = []
                v[':'.join(field)] = value
        except: # field not in row
            pass
    return [v]

def safe_reduce_infos(a, b, c, field):
    try:
        c[field] = list(set(a[field]+b[field]))
    except Exception as inst:
        try:
            c[field] = a[field]
            #print("[safe_reduce_infos: error] key error for '{}' for a".format(field))
        except Exception as inst2:
            try:
                c[field] = b[field]
                #print("[safe_reduce_infos: error] key error for '{}' for b".format(field))
            except Exception as inst3:
                c[field] = []
                print("[safe_reduce_infos: error] key error for '{}' for both a and b".format(field))
    return c


def safe_assign(a, c, field, fallback):
    if field in a:
        c[field] = a[field]
    else:
        print("[safe_assign: error] we have no {}.".format(field))
        c[field] = fallback
    return c


def test_info_s3_url(dict_img):
    return "info:s3_url" in dict_img and dict_img["info:s3_url"] and dict_img["info:s3_url"][0]!=u'None' and dict_img["info:s3_url"][0].startswith('https://s3') 


def reduce_sha1_infos_discarding_wimginfo(a, b):
    c = dict()
    if b:  # sha1 already existed
        if "info:image_discarded" in a or "info:image_discarded" in b:
            c["info:all_parent_ids"] = []
            c["info:image_discarded"] = 'discarded because has more than {} cdr_ids'.format(max_ads_image)
        else:
            c = safe_reduce_infos(a, b, c, "info:img_info")
            c = safe_reduce_infos(a, b, c, "info:all_parent_ids")
        if test_info_s3_url(a):
            c["info:s3_url"] = a["info:s3_url"]
        else:
            if test_info_s3_url(b):
                c["info:s3_url"] = b["info:s3_url"]
            else:
                print("[reduce_sha1_infos_discarding_wimginfo: error] both a and b have no s3 url.")
                c["info:s3_url"] = [None]
        # need to keep info:cu_feat_id if it exists
        if "info:cu_feat_id" in b:
            c["info:cu_feat_id"] = b["info:cu_feat_id"]
    else: # brand new image
        c = safe_assign(a, c, "info:s3_url", [None])
        c = safe_assign(a, c, "info:all_parent_ids", [])
        c = safe_assign(a, c, "info:img_info", [])
    # should discard if bigger than max_ads_image...
    if len(c["info:all_parent_ids"]) > max_ads_image:
        print("[reduce_sha1_infos_discarding_wimginfo: log] Discarding image with URL: {}".format(c["info:s3_url"][0]))
        c["info:all_parent_ids"] = []
        c["info:image_discarded"] = 'discarded because has more than {} cdr_ids'.format(max_ads_image)
    return c


def split_sha1_kv_images_discarded_wimginfo(x):
    # this prepares data to be saved in HBase
    tmp_fields_list = [("info","s3_url"), ("info","all_parent_ids"), ("info","img_info")]
    out = []
    if "info:image_discarded" in x[1] or len(x[1]["info:all_parent_ids"]) > max_ads_image_hbase:
        if "info:image_discarded" not in x[1]:
            x[1]["info:image_discarded"] = 'discarded because has more than {} cdr_ids'.format(max_ads_image_hbase)
        out.append((x[0], [x[0], "info", "image_discarded", x[1]["info:image_discarded"]]))
        str_s3url_value = None
        s3url_value = x[1]["info:s3_url"][0]
        str_s3url_value = unicode(s3url_value)
        out.append((x[0], [x[0], "info", "s3_url", str_s3url_value]))
        out.append((x[0], [x[0], "info", "all_parent_ids", x[1]["info:image_discarded"]]))
    else:
        for field in tmp_fields_list:
            if field[1]=="s3_url":
                out.append((x[0], [x[0], field[0], field[1], unicode(x[1][field[0]+":"+field[1]][0])]))
            elif field[1]=="img_info": 
                # deal with an older update that does not have this field.
                try:
                    out.append((x[0], [x[0], field[0], field[1], x[1][field[0]+":"+field[1]][0]]))
                except Exception:
                    pass
            else:
                out.append((x[0], [x[0], field[0], field[1], ','.join(x[1][field[0]+":"+field[1]])]))
    return out


def flatten_leftjoin(x):
    out = []
    # at this point value is a tuple of two lists with a single or empty dictionary
    c = reduce_sha1_infos_discarding_wimginfo(x[1][0],x[1][1])
    out.append((x[0], c))
    return out


def get_existing_joined_sha1(data):
    if len(data[1]) == 2 and data[1][1] and data[1][1] is not None and data[1][1] != 'None' and data[1][1] != u'None':
        return True
    return False


##-- New images for features computation functions
##---------------
def build_batch_out(batch_update, incr_update_id, batch_id):
    update_id = "index_update_"+incr_update_id+'_'+str(batch_id)
    list_key = []
    for x in batch_update:
        list_key.append(x)
    return [(update_id, [update_id, "info", "list_sha1s", ','.join(list_key)])]


def save_new_sha1s_for_index_update_batchwrite(new_sha1s_rdd, hbase_man_update_out, batch_update_size, incr_update_id, total_batches, nb_batchwrite=32):
    start_save_time = time.time()
    # use toLocalIterator if new_sha1s_rdd would be really big and won't fit in the driver's memory
    #iterator = new_sha1s_rdd.toLocalIterator()
    iterator = new_sha1s_rdd.collect()
    batch_update = []
    batch_out = []
    batch_id = 0
    push_batches = False
    for x in iterator:
        batch_update.append(x)
        if len(batch_update)==batch_update_size:
            if batch_id > 0 and batch_id % nb_batchwrite == 0:
                push_batches = True
            try:
                print("[save_new_sha1s_for_index_update_batchwrite] preparing batch {}/{} starting with: {}".format(batch_id+1, total_batches, batch_update[:10]))
                batch_out.extend(build_batch_out(batch_update, incr_update_id, batch_id))
                batch_id += 1
            except Exception as inst:
                print("[save_new_sha1s_for_index_update_batchwrite] Could not create/save batch {}. Error was: {}".format(batch_id, inst))
            batch_update = []
            if push_batches:
                batch_out_rdd = sc.parallelize(batch_out)
                print("[save_new_sha1s_for_index_update_batchwrite] saving {} batches of {} new images to HBase.".format(len(batch_out), batch_update_size))
                hbase_man_update_out.rdd2hbase(batch_out_rdd)
                batch_out = []
                push_batches = False

    # last batch
    if batch_update:
        try:    
            print("[save_new_sha1s_for_index_update_batchwrite] will prepare and save last batch {}/{} starting with: {}".format(batch_id+1, total_batches, batch_update[:10]))
            batch_out.extend(build_batch_out(batch_update, incr_update_id, batch_id))
            batch_out_rdd = sc.parallelize(batch_out)
            print("[save_new_sha1s_for_index_update_batchwrite] saving {} batches of {} new images to HBase.".format(len(batch_out), len(batch_update)))
            hbase_man_update_out.rdd2hbase(batch_out_rdd)
            #batch_rdd.unpersist()
        except Exception as inst:
            print("[save_new_sha1s_for_index_update_batchwrite] Could not create/save batch {}. Error was: {}".format(batch_id, inst))
    print("[save_new_sha1s_for_index_update_batchwrite] DONE in {}s".format(time.time() - start_save_time))


def save_new_images_for_index(basepath_save, out_rdd,  hbase_man_update_out, incr_update_id, c_options, new_images_to_index_str):
    batch_update_size = c_options.batch_update_size

    # save images without cu_feat_id that have not been discarded for indexing
    new_images_to_index = out_rdd.filter(lambda x: "info:image_discarded" not in x[1] and "info:cu_feat_id" not in x[1])
    
    new_images_to_index_count = new_images_to_index.count()
    print("[save_new_images_for_index] {}_count count: {}".format(new_images_to_index_str, new_images_to_index_count))
    save_info_incremental_update(hbase_man_update_out, incr_update_id, new_images_to_index_count, new_images_to_index_str+"_count")

    import numpy as np
    total_batches = int(np.ceil(np.float32(new_images_to_index_count)/batch_update_size))
    # partition to the number of batches?
    # 'save_new_sha1s_for_index_update' uses toLocalIterator()
    new_images_to_index_partitioned = new_images_to_index.partitionBy(total_batches)

    # save to HDFS too
    if c_options.save_inter_rdd:
        try:
            new_images_to_index_out_path = basepath_save + "/" + new_images_to_index_str
            if not hdfs_file_exist(new_images_to_index_out_path):
                print("[save_new_images_for_index] saving rdd to {}.".format(new_images_to_index_out_path))
                new_images_to_index_partitioned.keys().saveAsTextFile(new_images_to_index_out_path)
            else:
                print("[save_new_images_for_index] skipped saving rdd to {}. File already exists.".format(new_images_to_index_out_path))
            save_info_incremental_update(hbase_man_update_out, incr_update_id, new_images_to_index_out_path, new_images_to_index_str+"_path")
        except Exception as inst:
            print("[save_new_images_for_index] could not save rdd 'new_images_to_index' at {}, error was {}.".format(new_images_to_index_out_path, inst))

    # save by batch in HBase to let the API know it needs to index these images    
    print("[save_new_images_for_index] start saving by batches of {} new images.".format(batch_update_size))
    # crashes in 'save_new_sha1s_for_index_update'?
    #save_new_sha1s_for_index_update(new_images_to_index_partitioned.keys(), hbase_man_update_out, batch_update_size, incr_update_id, total_batches)
    save_new_sha1s_for_index_update_batchwrite(new_images_to_index_partitioned.keys(), hbase_man_update_out, batch_update_size, incr_update_id, total_batches)
    

##---------------
##-- END New images for features computation functions


##-- Amandeep RDDs I/O
##---------------

def out_to_amandeep_dict_str_wimginfo(x):
    # this is called with map()
    sha1 = x[0]
    # keys should be: "image_sha1", "all_parent_ids", "s3_url", "img_info"
    # keep "cu_feat_id" to be able to push images to be indexed
    out_dict = dict()
    out_dict["image_sha1"] = sha1
    # use "for field in fields_list:" instead? and ':'.join(field)
    for field in ["all_parent_ids", "s3_url", "cu_feat_id", "img_info"]:
        if "info:"+field in x[1]:
            out_dict[field] = x[1]["info:"+field]
    return (sha1, json.dumps(out_dict))


def amandeep_dict_str_to_out_wimginfo(x):
    # this is called with mapValues()
    # keys should be: "image_sha1", "all_parent_ids", "s3_url", "all_cdr_ids", "img_info"
    # keep "cu_feat_id" to be able to push images to be indexed
    tmp_dict = json.loads(x)
    out_dict = dict()
    #sha1 = tmp_dict["image_sha1"]
    for field in ["all_parent_ids", "s3_url", "cu_feat_id", "img_info"]:
        if field in tmp_dict:
            out_dict["info:"+field] = tmp_dict[field]
    return out_dict


def filter_out_rdd(x):
    return "info:image_discarded" not in x[1] and len(x[1]["info:all_parent_ids"]) <= max_ads_image_dig

##-- END Amandeep RDDs I/O
##---------------


##-- Incremental update get RDDs main functions
##---------------
def build_query_CDR(es_ts_start, es_ts_end, c_options):
    print("Will query CDR from {} to {}".format(es_ts_start, es_ts_end))

    if es_ts_start is not None:
        gte_range = "\"gte\" : "+str(es_ts_start)
    else:
        gte_range = "\"gte\" : "+str(0)
    if es_ts_end is not None:
        lt_range = "\"lt\": "+str(es_ts_end)
    else:
        # max_ts or ts of now?
        lt_range = "\"lt\": "+str(max_ts)
    # build range ts
    range_timestamp = "{\"range\" : {\"_timestamp\" : {"+",".join([gte_range, lt_range])+"}}}"
    # build query
    query = None
    # will depend on c_options.cdr_format too
    if c_options.cdr_format == 'v2':
        query = "{\"fields\": [\""+"\", \"".join(fields_cdr)+"\"], \"query\": {\"filtered\": {\"query\": {\"match\": {\"content_type\": \"image/jpeg\"}}, \"filter\": "+range_timestamp+"}}, \"sort\": [ { \"_timestamp\": { \"order\": \"asc\" } } ] }"
    elif c_options.cdr_format == 'v3':
        print "[build_query_CDR: ERROR] CDR format v3 not yet supported."
        # need to get fields objects.fields_cdr?
        # and match: {"objects.content_type": "image/jpeg"?
    else:
        print "[build_query_CDR: ERROR] Unkown CDR format: {}".format(options.cdr_format)
    return query


def get_s3url_adid_rdd(basepath_save, es_man, es_ts_start, es_ts_end, hbase_man_update_out, ingestion_id, options, start_time):
    rdd_name = "s3url_adid_rdd"
    prefnout = "get_s3url_adid_rdd: "

    # Try to load from disk (always? or only if c_options.restart is true?)
    if c_options.restart:
        s3url_adid_rdd = load_rdd_json(basepath_save, rdd_name)
        if s3url_adid_rdd is not None:
            print("[{}log] {} loaded rdd from {}.".format(prefnout, rdd_name, basepath_save + "/" + rdd_name))
            return s3url_adid_rdd

    # Format query to ES to get images
    query = build_query_CDR(es_ts_start, es_ts_end, c_options)
    if query is None:
        print("[{}log] empty query...".format(prefnout))
        return None
    print("[{}log] query CDR: {}".format(prefnout, query))
    
    # Actually get images
    es_rdd_nopart = es_man.es2rdd(query)
    if es_rdd_nopart.isEmpty():
        print("[{}log] empty ingestion...".format(prefnout))
        return None

    # es_rdd_nopart is likely to be underpartitioned
    es_rdd_count = es_rdd_nopart.count()
    # should we partition based on count and max_samples_per_partition?
    es_rdd = es_rdd_nopart.partitionBy(get_partitions_nb(options, es_rdd_count))

    # save ingestion infos
    ingestion_infos_list = []
    ingestion_infos_list.append((ingestion_id, [ingestion_id, "info", "start_time", str(start_time)]))
    ingestion_infos_list.append((ingestion_id, [ingestion_id, "info", "es_rdd_count", str(es_rdd_count)]))
    ingestion_infos_rdd = sc.parallelize(ingestion_infos_list)
    hbase_man_update_out.rdd2hbase(ingestion_infos_rdd)

    # transform to (s3_url, adid) format
    s3url_adid_rdd = None
    if c_options.cdr_format == 'v2':
        s3url_adid_rdd = es_rdd.flatMap(CDRv2_to_s3url_adid)
    elif c_options.cdr_format == 'v3':
        s3url_adid_rdd = es_rdd.flatMap(CDRv3_to_s3url_adid)
    else:
        print "[get_s3url_adid_rdd: ERROR] Unkown CDR format: {}".format(options.cdr_format)

    if c_options.save_inter_rdd:
        save_rdd_json(basepath_save, rdd_name, s3url_adid_rdd, ingestion_id, hbase_man_update_out)
    return s3url_adid_rdd


def save_out_rdd_to_hdfs(basepath_save, out_rdd, hbase_man_update_out, ingestion_id, rdd_name):
    out_rdd_path = basepath_save + "/" + rdd_name
    try:
        if not hdfs_file_exist(out_rdd_path):
            out_rdd_save = out_rdd.filter(filter_out_rdd).map(out_to_amandeep_dict_str_wimginfo)
            if not out_rdd_save.isEmpty():
                # how to force overwrite here?
                #out_rdd_save.saveAsSequenceFile(out_rdd_path)
                out_rdd_save.saveAsSequenceFile(out_rdd_path, compressionCodecClass=compression)
                save_info_incremental_update(hbase_man_update_out, ingestion_id, out_rdd_path, rdd_name+"_path")
            else:
                print("[save_out_rdd_to_hdfs] 'out_rdd_save' is empty.")
                save_info_incremental_update(hbase_man_update_out, ingestion_id, "EMPTY", rdd_name+"_path")
        else:
            print "[save_out_rdd_to_hdfs] Skipped saving out_rdd. File already exists at {}.".format(out_rdd_path)
    except Exception as inst:
        print "[save_out_rdd_to_hdfs: error] Error when trying to save out_rdd to {}. {}".format(out_rdd_path, inst)


def save_out_rdd_to_hbase(out_rdd, hbase_man_sha1infos_out):
    if out_rdd is not None:
        # write out rdd of new images 
        out_rdd_hbase = out_rdd.flatMap(split_sha1_kv_images_discarded_wimginfo)
        if not out_rdd_hbase.isEmpty():
            print("[save_out_rdd_to_hbase] saving 'out_rdd' to sha1_infos HBase table.")
            hbase_man_sha1infos_out.rdd2hbase(out_rdd_hbase)
            # how to be sure this as completed?
        else:
            print("[save_out_rdd_to_hbase] 'out_rdd' is empty.")
    else:
        print("[save_out_rdd_to_hbase] 'out_rdd' is None.")

##-------------

def join_ingestion(hbase_man_sha1infos_join, ingest_rdd, options, ingest_rdd_count):
    # update parents cdr_ids for existing sha1s
    print("[join_ingestion] reading from hbase_man_sha1infos_join to get sha1_infos_rdd.")
    sha1_infos_rdd = hbase_man_sha1infos_join.read_hbase_table()
    # we may need to merge some 'all_parent_ids'
    if not sha1_infos_rdd.isEmpty(): 
        sha1_infos_rdd_count = sha1_infos_rdd.count()
        # use get_partitions_nb(options, rdd_count)
        nb_partitions_ingest = get_partitions_nb(options, ingest_rdd_count)
        nb_partitions_sha1_infos = get_partitions_nb(options, sha1_infos_rdd_count)
        # partitioned rdd in the same number of partitions to minmize shuffle in the leftOuterJoin
        nb_partitions = max(nb_partitions_sha1_infos, nb_partitions_ingest)
        #sha1_infos_rdd_json = sha1_infos_rdd.flatMap(sha1_key_json).partitionBy(nb_partitions)
        sha1_infos_rdd_json = sha1_infos_rdd.partitionBy(nb_partitions).flatMapValues(sha1_key_json_values)
        ingest_rdd_partitioned = ingest_rdd.partitionBy(nb_partitions)
        join_rdd = ingest_rdd_partitioned.leftOuterJoin(sha1_infos_rdd_json).flatMap(flatten_leftjoin)
        out_rdd = join_rdd
    else: # first update
        out_rdd = ingest_rdd
    return out_rdd


def run_ingestion(es_man, hbase_man_sha1infos_join, hbase_man_sha1infos_out, hbase_man_update_out, ingestion_id, es_ts_start, es_ts_end, c_options):
    
    print max_ads_image

    restart = c_options.restart
    batch_update_size = c_options.batch_update_size

    start_time = time.time()
    basepath_save = c_options.base_hdfs_path+ingestion_id+'/images/info'
    
    # get images from CDR, output format should be (s3_url, ad_id)
    # NB: later on we will load from disk from another job
    s3url_adid_rdd = get_s3url_adid_rdd(basepath_save, es_man, es_ts_start, es_ts_end, hbase_man_update_out, ingestion_id, c_options, start_time)

    if s3url_adid_rdd is None:
        print "No data retrieved!"
        return

    # reduce by key to download each image once
    s3url_adid_rdd_red = s3url_adid_rdd.flatMapValues(lambda x: [[x]]).reduceByKey(reduce_s3url_listadid)
    s3url_adid_rdd_red_count = s3url_adid_rdd_red.count()
    save_info_incremental_update(hbase_man_update_out, ingestion_id, s3url_adid_rdd_red_count, "s3url_adid_rdd_red_count")

    # process (compute SHA1, and reduce by SHA1)
    # repartition first based on s3url_adid_rdd_red_count?
    # this could be done as a flatMapValues
    # s3url_adid_rdd_red.partitionBy(get_partitions_nb(c_options, s3url_adid_rdd_red_count)).flatMapValues(...)
    s3url_infos_rdd = s3url_adid_rdd_red.flatMap(check_get_sha1_imginfo_s3url)
    
    print '[s3url_infos_rdd: first] {}'.format(s3url_infos_rdd.first())
    # transform to (SHA1, imginfo)
    sha1_infos_rdd = s3url_infos_rdd.flatMap(s3url_listadid_sha1_imginfo_to_sha1_alldict)
    print '[sha1_infos_rdd: first] {}'.format(sha1_infos_rdd.first())
    ingest_rdd = sha1_infos_rdd.reduceByKey(reduce_sha1_infos_discarding_wimginfo)
    print '[ingest_rdd: first] {}'.format(ingest_rdd.first())
    ingest_rdd_count = ingest_rdd.count()
    save_info_incremental_update(hbase_man_update_out, ingestion_id, ingest_rdd_count, "ingest_rdd_count")

    # save to disk
    if c_options.save_inter_rdd:
        save_rdd_json(basepath_save, "ingest_rdd", ingest_rdd, ingestion_id, hbase_man_update_out)

    # join with existing sha1
    out_rdd = join_ingestion(hbase_man_sha1infos_join, ingest_rdd, c_options, ingest_rdd_count)
    print '[out_rdd: first] {}'.format(out_rdd.first())
    save_out_rdd_to_hdfs(basepath_save, out_rdd, hbase_man_update_out, ingestion_id, "out_rdd")
    save_out_rdd_to_hbase(out_rdd, hbase_man_sha1infos_out)

    if out_rdd is not None and not out_rdd.isEmpty():
        save_new_images_for_index(basepath_save, out_rdd, hbase_man_update_out, ingestion_id, c_options, "new_images_to_index")

    update_elapsed_time = time.time() - start_time 
    save_info_incremental_update(hbase_man_update_out, ingestion_id, str(update_elapsed_time), "update_elapsed_time")


def get_ingestion_start_end_id(c_options):

    es_ts_start = None
    es_ts_end = None
    ingestion_id = None

    # Get es_ts_start and es_ts_end
    if c_options.es_ts_start is not None:
        es_ts_start = c_options.es_ts_start
    if c_options.es_ts_end is not None:
        es_ts_end = c_options.es_ts_end
    if c_options.es_ts_start is None and c_options.es_ts_end is None and c_options.day_to_process is not None:
        # Compute for day to process
        import calendar
        import dateutil.parser
        try:
            start_date = dateutil.parser.parse(c_options.day_to_process)
            es_ts_end = calendar.timegm(start_date.utctimetuple())*1000
            es_ts_start = es_ts_end - day_gap
            # Should use es_ts_end to build ingestion_id to be consistent with previous version of the workflow
            ingestion_id = datetime.date.fromtimestamp((es_ts_end) / 1000).isoformat()
        except Exception as inst:
            print "[get_ingestion_start_end_id: log] Could not parse 'day_to_process'. Getting everything form the CDR."

    # Otherwise consider we want ALL images
    if es_ts_start is None:
        es_ts_start = 0
    if es_ts_end is None:
        es_ts_end = max_ts

    # Form ingestion id if not set yet
    if ingestion_id is None:
        ingestion_id = '-'.join([c_options.es_domain, str(es_ts_start), str(es_ts_end)])

    return es_ts_start, es_ts_end, ingestion_id


## MAIN
if __name__ == '__main__':
    start_time = time.time()

    # Setup parser for arguments options
    parser = ArgumentParser()

    # Define groups
    job_group = parser.add_argument_group("job", "Job related parameters")
    hbase_group = parser.add_argument_group("hbase", "HBase related parameters")
    es_group = parser.add_argument_group("es", "ElasticSearch related parameters")

    # Define HBase related arguments
    hbase_group.add_argument("--hbase_host", dest="hbase_host", required=True)
    hbase_group.add_argument("--hbase_port", dest="hbase_port", default=2181)
    hbase_group.add_argument("--hbase_ip", dest="hbase_ip", default="10.1.94.57")
    # BEWARE: these tables should be already created
    # we could just have a table_prefix
    hbase_group.add_argument("--table_sha1", dest="tab_sha1_infos_name", required=True)
    hbase_group.add_argument("--table_update", dest="tab_update_name", required=True)

    # Define ES related options
    es_group.add_argument("--es_host", dest="es_host", required=True)
    es_group.add_argument("--es_domain", dest="es_domain", required=True)
    es_group.add_argument("--es_user", dest="es_user", required=True)
    es_group.add_argument("--es_pass", dest="es_pass", required=True)
    es_group.add_argument("--es_port", dest="es_port", default=9200)
    es_group.add_argument("--es_index", dest="es_index", default='memex-domains')
    es_group.add_argument("--es_ts_start", dest="es_ts_start", help="start timestamp in ms", default=None)
    es_group.add_argument("--es_ts_end", dest="es_ts_end", help="end timestamp in ms", default=None)
    es_group.add_argument("--cdr_format", dest="cdr_format", choices=['v2', 'v3'], default='v2')
    
    # Define job related options
    job_group.add_argument("-r", "--restart", dest="restart", default=False, action="store_true")
    job_group.add_argument("-i", "--identifier", dest="identifier")
    job_group.add_argument("-s", "--save", dest="save_inter_rdd", default=False, action="store_true")
    job_group.add_argument("-b", "--batch_update_size", dest="batch_update_size", type=int, default=default_batch_update_size)
    job_group.add_argument("--max_ads_image_dig", dest="max_ads_image_dig", type=int, default=max_ads_image_dig)
    job_group.add_argument("--max_ads_image_hbase", dest="max_ads_image_hbase", type=int, default=max_ads_image_hbase)
    # should this be estimated from RDD counts actually?
    #job_group.add_argument("-p", "--nb_partitions", dest="nb_partitions", type=int, default=480)
    job_group.add_argument("-p", "--nb_partitions", dest="nb_partitions", type=int, default=-1)
    job_group.add_argument("-d", "--day_to_process", dest="day_to_process", help="using format YYYY-MM-DD", default=None)
    job_group.add_argument("--max_samples_per_partition", dest="max_samples_per_partition", type=int, default=max_samples_per_partition)
    job_group.add_argument("--base_hdfs_path", dest="base_hdfs_path", default=base_hdfs_path)
    
    # Parse
    try:
        c_options = parser.parse_args()
        print "Got options:", c_options
        max_ads_image_dig = c_options.max_ads_image_dig
        max_ads_image_hbase = c_options.max_ads_image_hbase
        max_ads_image = max(max_ads_image_dig, max_ads_image_hbase)
    except Exception as inst:
        print inst
        parser.print_help()
    
    es_ts_start, es_ts_end, ingestion_id = get_ingestion_start_end_id(c_options)


    # Setup SparkContext    
    sc = SparkContext(appName="getimages-"+ingestion_id+dev_release_suffix)
    conf = SparkConf()
    log4j = sc._jvm.org.apache.log4j
    log4j.LogManager.getRootLogger().setLevel(log4j.Level.ERROR)
    
    # Setup HBase managers
    join_columns_list = [':'.join(x) for x in fields_list]
    get_create_table(c_options.tab_sha1_infos_name, c_options)
    hbase_fullhost = c_options.hbase_host+':'+str(c_options.hbase_port)
    hbase_man_sha1infos_join = HbaseManager(sc, conf, hbase_fullhost, c_options.tab_sha1_infos_name, columns_list=join_columns_list)
    hbase_man_sha1infos_out = HbaseManager(sc, conf, hbase_fullhost, c_options.tab_sha1_infos_name)
    get_create_table(c_options.tab_update_name, c_options)
    hbase_man_update_out = HbaseManager(sc, conf, hbase_fullhost, c_options.tab_update_name)
    
    # Setup ES manager
    es_man = ES(sc, conf, c_options.es_index, c_options.es_domain, c_options.es_host, c_options.es_port, c_options.es_user, c_options.es_pass)
    es_man.set_output_json()
    es_man.set_read_metadata()

    # Run update
    print "[START] Starting ingestion {}".format(ingestion_id)
    run_ingestion(es_man, hbase_man_sha1infos_join, hbase_man_sha1infos_out, hbase_man_update_out, ingestion_id, es_ts_start, es_ts_end, c_options)
    print "[DONE] Ingestion {} done in {}s.".format(ingestion_id, time.time() - start_time)

