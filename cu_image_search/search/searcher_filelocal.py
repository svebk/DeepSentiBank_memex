import sys
import time
import json
import numpy as np
from collections import OrderedDict
from ..memex_tools.sha1_tools import get_SHA1_from_file


class SearcherFileLocal():

    def __init__(self,global_conf_filename):
        self.global_conf_filename = global_conf_filename
        self.global_conf = json.load(open(global_conf_filename,'rt'))
        self.read_conf()
        self.init_indexer()
        self.init_ingester()

    def read_conf(self):
        # these parameters may be overwritten by web call
        self.features_dim = self.global_conf['FE_features_dim']
        self.sim_limit = self.global_conf['SE_sim_limit']
        self.near_dup = self.global_conf['SE_near_dup']
        self.near_dup_th =  self.global_conf['SE_near_dup_th']
        self.ratio = self.global_conf['SE_ratio']
        self.topfeature = 0
        if "SE_topfeature" in self.global_conf:
            self.topfeature = int(self.global_conf['SE_topfeature'])

    def init_indexer(self):
        """ Initialize `indexer` from `global_conf['SE_indexer']` value.

        Currently supported indexer types are:
        - local_indexer
        - hbase_indexer
        """
        field = 'SE_indexer'
        if field not in self.global_conf:
            raise ValueError("[Searcher: error] "+field+" is not defined in configuration file.")
        if self.global_conf[field]=="local_indexer":
            from ..indexer.local_indexer import LocalIndexer
            self.indexer = LocalIndexer(self.global_conf_filename)
        elif self.global_conf[field]=="hbase_indexer":
            from ..indexer.hbase_indexer import HBaseIndexer
            self.indexer = HBaseIndexer(self.global_conf_filename)
        else:
            raise ValueError("[Searcher: error] unknown 'indexer' {}.".format(self.global_conf[field]))

    def init_ingester(self):
        """ Initialize `ingester` from `global_conf['SE_ingester']` value.

        Currently supported indexer types are:
        - local_ingester
        - hbase_indexer
        """
        field = 'SE_ingester'
        if field not in self.global_conf:
            raise ValueError("[Searcher: error] "+field+" is not defined in configuration file.")
        if self.global_conf[field]=="local_ingester":
            from ..ingester.local_ingester import LocalIngester
            self.ingester = LocalIngester(self.global_conf_filename)
        else:
            raise ValueError("[Searcher: error] unknown 'ingester' {}.".format(self.global_conf[field]))

    def check_ratio(self):
        '''Check if we need to set the ratio based on topfeature.'''
        if self.topfeature > 0:
            self.ratio = self.topfeature*1.0/self.indexer.get_nb_images_indexed()
            msg = "[Searcher.check_ratio: log] Set ratio to {} as we want top {} images out of {} indexed."
            print msg.format(self.ratio, self.topfeature, self.indexer.get_nb_images_indexed())


    def filter_near_dup(self,nums):
        # nums is a list of ids then distances
        # onum is the number of similar images
        onum = len(nums)/2
        temp_nums=[]
        #print "[Searcher.filter_near_dup: log] nums {}".format(nums)
        for one_num in range(0,onum):
            # maintain only near duplicates, i.e. distance less than self.near_dup_th
            if float(nums[onum+one_num])>self.near_dup_th:
                return temp_nums
            # insert id at its right place
            temp_nums.insert(one_num,nums[one_num])
            # insert corresponding distance at the end
            temp_nums.insert(len(temp_nums),nums[onum+one_num])
        #print "[Searcher.filter_near_dup: log] temp_nums {}".format(temp_nums)
        return temp_nums


    def read_sim(self, simname, nb_query):
        # initialization
        sim = []
        sim_score = []
        
        # read similar images
        count = 0
        f = open(simname);
        for line in f:
            #sim_index.append([])
            nums = line.replace(' \n','').split(' ')
            if self.near_dup: #filter near duplicate here
                nums=self.filter_near_dup(nums)
            #print nums
            onum = len(nums)/2
            n = min(self.sim_limit,onum)
            #print n
            if n==0: # no returned images, e.g. no near duplicate
                sim.append(())
                sim_score.append([])
                continue
            sim.append(self.indexer.get_sim_infos(nums[0:n]))
            sim_score.append(nums[onum:onum+n])
            count = count + 1
            if count == nb_query:
                break
        f.close()
        return sim,sim_score

    def format_output(self, simname, list_sha1_id, nb_query, corrupted):
        # read hashing similarity results
        sim, sim_score = self.read_sim(simname, nb_query)

        # build final output
        output = []
        dec = 0
        for i in range(0,nb_query):    
            output.append(dict())
            output[i]['query_sha1'] = list_sha1_id[i]
            if i in corrupted:
                output[i]['similar_images']= OrderedDict([['number',0],['image_urls',[]],['cached_image_urls',[]],['page_urls',[]],['ht_ads_id',[]],['ht_images_id',[]],['sha1',[]],['distance',[]]])
                dec += 1
                continue
            ii = i - dec
            output[i]['similar_images']= OrderedDict([['number',len(sim[ii])],['image_urls',[]],['cached_image_urls',[]],['page_urls',[]],['ht_ads_id',[]],['ht_images_id',[]],['sha1',[]],['distance',[]]])
            for simj in sim[ii]:
                url = simj[0]
                #print url, self.ingester.host_data_dir, self.ingester.data_dir
                if not url.startswith('http'):
                    # This will not work, need to serve static files.
                    url = "/show_image/image?data="+url
                #print url, self.ingester.host_data_dir, self.ingester.data_dir
                output[i]['similar_images']['image_urls'].append(url)
                output[i]['similar_images']['cached_image_urls'].append(url)
                output[i]['similar_images']['page_urls'].append(simj[2])
                output[i]['similar_images']['ht_ads_id'].append(simj[3])
                output[i]['similar_images']['ht_images_id'].append(simj[4])
                output[i]['similar_images']['sha1'].append(simj[5])
            output[i]['similar_images']['distance']=sim_score[ii]
        #print "[Searcher.format_output: log] output {}".format(output)
        outp = OrderedDict([['number',nb_query],['images',output]])
        #print "[Searcher.format_output: log] outp {}".format(outp)
        #json.dump(outp, open(outputname,'w'),indent=4, sort_keys=False)
        return  outp

    def search_one_imagepath(self, image_path):
        # initialization
        search_id = str(time.time())
        all_img_filenames = [image_path]
        return self.search_from_image_filenames(all_img_filenames, search_id)
        
    def search_image_list(self, query_urls, options_dict):
        # initialization
        search_id = str(time.time())

        # read list of images
        all_img_filenames = [None]*len(query_urls)
        URL_images = []
        for pos,image in enumerate(query_urls):
            if image[0:4] == "http":
                URL_images.append((pos,image))
            else:
                all_img_filenames[pos] = image

        if URL_images:
            readable_images = self.indexer.image_downloader.download_images(URL_images, search_id)
            print readable_images
            for img_tup in readable_images:
                # print "[Searcher.search_image_list: log] {} readable image tuple {}.".format(i,img_tup)
                all_img_filenames[img_tup[0]] = img_tup[-1]

        print "all_img_filenames: ",all_img_filenames

        return self.search_from_image_filenames(all_img_filenames, search_id, options_dict)

    def search_from_image_filenames(self, all_img_filenames, search_id, options_dict):
        # compute all sha1s
        corrupted = []
        list_sha1_id = []
        valid_images = []
        for i, image_name in enumerate(all_img_filenames):
            if image_name[0:4]!="http":
                sha1 = get_SHA1_from_file(image_name)
                if sha1:
                    list_sha1_id.append(sha1)
                    valid_images.append((i, sha1, image_name))
                else:
                    corrupted.append(i)
            else: # we did not manage to download image
                # need to deal with that in output formatting too
                corrupted.append(i)

        print "valid_images",valid_images
        sys.stdout.flush()
        #print "[Searcher.search_from_image_filenames: log] valid_images {}".format(valid_images)
        # get indexed images
        list_ids_sha1_found = self.indexer.get_ids_from_sha1s(list_sha1_id)
        tmp_list_ids_found = [x[0] for x in list_ids_sha1_found]
        list_sha1_found = [x[1] for x in list_ids_sha1_found]
        #print "[Searcher.search_from_image_filenames: log] list_sha1_id {}".format(list_sha1_id)
        #print "[Searcher.search_from_image_filenames: log] list_sha1_found {}".format(list_sha1_found)
        # this is to keep proper ordering
        list_ids_found = [tmp_list_ids_found[list_sha1_found.index(sha1)] for sha1 in list_sha1_id if sha1 in list_sha1_found]
        #print "[Searcher.search_from_image_filenames: log] tmp_list_ids_found {}".format(tmp_list_ids_found)
        #print "[Searcher.search_from_image_filenames: log] list_ids_found {}".format(list_ids_found)
        # get there features
        if list_ids_found:
            feats, ok_ids = self.indexer.hasher.get_precomp_feats(list_ids_found)
            if len(ok_ids)!=len(list_ids_found):
                raise ValueError("[Searcher.search_from_image_filenames: error] We did not get enough precomputed features ({}) from list of {} images.".format(len(ok_ids),len(list_ids_found)))
        # compute new images features
        not_indexed_sha1 = set(list_sha1_id)-set(list_sha1_found)
        #res = self.indexer.get_precomp_from_sha1(list_ids_sha1_found)
        new_files = []
        all_valid_images = []
        precomp_img_filenames=[]
        for i, sha1, image_name in valid_images:
            if sha1 in list_sha1_found: # image is indexed
                precomp_img_filenames.append(image_name)
            else:
                new_files.append(image_name)
            all_valid_images.append(all_img_filenames[i])
        print "[Searcher.search_from_image_filenames: log] all_valid_images {}".format(all_valid_images)
        print "[Searcher.search_from_image_filenames: log] new_files {}".format(new_files)
        sys.stdout.flush()
        features_filename, ins_num = self.indexer.feature_extractor.compute_features(new_files, search_id)
        if ins_num!=len(new_files):
            raise ValueError("[Searcher.search_from_image_filenames: error] We did not get enough features ({}) from list of {} images.".format(ins_num,len(new_files)))
        # merge feats with features_filename
        final_featuresfile = search_id+'.dat'
        read_dim = self.features_dim*4
        read_type = np.float32
        #print "[Searcher.search_from_image_filenames: log] feats {}".format(feats)
        with open(features_filename,'rb') as new_feats, open(final_featuresfile,'wb') as out:
            for image_name in all_valid_images:
                #print "[Searcher.search_from_image_filenames: log] saving feature of image {}".format(image_name)
                if image_name in precomp_img_filenames:
                    # select precomputed 
                    precomp_pos = precomp_img_filenames.index(image_name)
                    #print "[Searcher.search_from_image_filenames: log] getting precomputed feature at position {}".format(precomp_pos)
                    tmp_feat = feats[precomp_pos][:]
                else:
                    # read from new feats
                    tmp_feat = np.frombuffer(new_feats.read(read_dim),dtype=read_type)
                # Should tmp_feat be normalized?
                print "tmp_feat",tmp_feat
                tmp_feat = tmp_feat/np.linalg.norm(tmp_feat)
                print "tmp_feat normed", tmp_feat
                sys.stdout.flush()
                out.write(tmp_feat)
        # query with merged features_filename
        self.check_ratio()
        simname = self.indexer.hasher.get_similar_images_from_featuresfile(final_featuresfile, self.ratio)
        #outputname = simname[:-4]+".json"
        outp = self.format_output(simname, list_sha1_id, len(all_img_filenames), corrupted)
        #return outp, outputname
        return outp
