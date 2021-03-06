import sys, struct, os, time, types, re, math, random, operator, hashlib, pdb
import logging, settings
logging.basicConfig(filename=settings.LOG_FILENAME, level=logging.DEBUG)

from lsh.shingles.shingles import _get_list_of_shingles
from lsh.utils.similarity import compute_positive_hash

DbType = settings.DATABASES['default']['ENGINE']
if DbType == 'cassandra':
    from db_cassandra import DbInt, Table
elif DbType == 'datastore':
    from db_datastore import DbInt, Table
else:
    from db_in_memory import DbInt, Table

class UnknownException(Exception):
    pass

class Matrix(object):
    __metaclass__ = Table
    attrs = [
             'ds_key text',
             'source text',
             'filename text',
             'file_key text',
             'lsh_output text',
             'eval_output text',
             'count_output text',
             'random_seeds list<bigint>',
             'buckets list<int>',
             'rows int',
             'bands int',
             'shingle_type ascii',
             'minhash_modulo int',
             ]
    p_keys = ['ds_key']

    def __init__(self):
        return

    @classmethod
    def get(cls, ds_key):
        cls._initialize()
        if ds_key:
            ds = cls.select_row(ds_key = ds_key)
            if ds:
                for attr in ds:
                    if attr in ('random_seeds', 'buckets'):
                        if ds[attr]:
                            logging.info('retrieved dataset[%s][0] type %s, value %s', attr, type(ds[attr][0]), settings.max_mask & ds[attr][0])
                    else:
                        logging.info('retrieved dataset[%s] type %s, value %s', attr, type(ds[attr]), ds[attr])
                return ds
        return None
    
    @classmethod
    def find(cls, ds_key):
        cls._initialize()
        matrix = Matrix.select_row(ds_key = ds_key)
        if not matrix:
            logging.warning('Matrix.find failed to find matrix with ds_key %s', ds_key)
        return matrix

    def find_child_rows(self):
        MatrixRow._initialize()
        return MatrixRow.select_all(parent = self)

    @classmethod
    def _initialize(cls):
        matrix = Matrix(name = cls.__name__, attrs = cls.attrs, p_keys = cls.p_keys)
        return matrix

    @classmethod
    def make_new_id(cls, source, filename):
        cls._initialize()
        max_iters = 4
        for iter_count in xrange(max_iters):
            ds_key = 'k%04d' % (int(hashlib.md5(source + filename + ' ' * iter_count).hexdigest(), 16) % 10000)
            try:
                # Does a dataset with this ID already exist?
                this_ds = Matrix.select_row(ds_key = ds_key)
                if not this_ds:
                    break
                if this_ds.filename == filename:
                    logging.debug("A dataset with %s already exists, reusing", filename)
                    return ds_key
            except ValueError:
                raise Exception('WTF?')
        if iter_count == max_iters - 1:
            raise Exception("Unable to create Dataset ID")
        return ds_key
    
    @classmethod
    def create(cls, source, filename, file_key = '',
               rows = settings.rows, 
               bands = settings.bands, 
               shingle_type = settings.shingle_type, 
               minhash_modulo = settings.minhash_modulo):

#         logging.debug('Matrix.create cls = %s, vars = %s', cls, vars(cls))
        Matrix._initialize()

#         logging.debug('Matrix.create inputs %s, %s, %s', source, filename, file_key)        
        ds_key = cls.make_new_id(source, filename)
#         logging.debug('Matrix.create ds_key %s', ds_key)

        max_hashes = rows * bands
        data = {
                'ds_key': '%s' % ds_key,
                'source': '%s' % source,
                'filename': '%s' % filename,
                'file_key': '%s' % file_key,
                'random_seeds': [(settings.max_mask & random.getrandbits(settings.max_bits)) for _ in xrange(max_hashes)],
                'rows': rows,
                'bands': bands,
                'shingle_type': '%s' % shingle_type,
                'minhash_modulo': minhash_modulo,
                }
        Matrix.insert_row(data = data)
        matrix = Matrix.find(ds_key)
#         logging.debug('Matrix.create returning %s', matrix)
        return matrix

    def str(self):
        txt = '<Matrix ds_key={ds_key} />'.format(ds_key = self.ds_key)
        return txt

    def get_else_create_doc(self, doc_id):
        try:
            doc = MatrixRow.select_row(ds_key = self.ds_key, doc_id = doc_id)
            if doc:
                return True, doc
        except:
            pass
        doc = MatrixRow(name = 'MatrixRow', attrs = MatrixRow.attrs, p_keys = MatrixRow.p_keys)
        doc.ds_key = self.ds_key
        doc.doc_id = doc_id
        return False, doc

    def get_doc(self, doc_id):
        try:
            doc = MatrixRow.select_row(ds_key = self.ds_key, doc_id = doc_id)
            if doc:
                doc.ds_key = self.ds_key
                doc.doc_id = doc_id
                return doc
        except:
            pass
        return None

    def docs_iterator(self):
        return MatrixRow.select_all(ds_key = self.ds_key)

    def purge(self):
        MatrixRow._initialize()
        MatrixRow.delete_all(parent = self)
        Matrix.delete_row(ds_key = self.ds_key)

    def get_nns(self, doc_id):
        doc = self.get_doc(doc_id)
        if not doc:
            return []
        bkts = [DbInt.fm_db(bkt) for bkt in doc.buckets]
        mhs = {}
        for bkt in bkts:
            bkt_docs = session.execute(self.nns_select, [self.ds_key, DbInt.to_db(bkt)])
            for bkt_doc in bkt_docs:
                mhs[bkt_doc['doc_id']] = bkt_doc['minhashes']
        del mhs[doc_id]
        jac = {}
        for doc_id2 in mhs.keys():
            jac_min = reduce(lambda x, y: x+y, map(lambda a,b: a == b, doc.minhashes,mhs[doc_id2])) / float(len(doc.minhashes))
            jac[doc_id2] = 1.0 - jac_min
            if 0 == int(1000*time.time()) % 100:
                logging.info('Sampling (1%%) Jaccard distance %s | %s: %6.2f', doc_id, doc_id2, jac[doc_id2])
        return jac

    def create_doc(self, _id, text, stats):
        (found, doc) = self.get_else_create_doc(_id)
        stats['found'] = found
        if found:
            # if 0 == int(1000*time.time()) % 20:
            #     # print 5% of the documents on average
            #     logging.info('%s %s',doc['ds_key'], doc['doc_id'])
            return doc

        ### Parse
        t0 = time.time()
        doc.text = text
        tParse = time.time() - t0
        stats['parse'] = tParse
        doc.dataset = self
        doc.rows = self.rows
        doc.hashes = doc.rows * self.bands
        doc.seeds = list(self.random_seeds)
        doc.modulo = self.minhash_modulo
        doc.sh_type = self.shingle_type

        max_hashes = self.rows * self.bands
        doc.minhashes = doc.calc_minhashes()
        tMinhash = time.time() - t0 - tParse
        stats['minhash'] = tMinhash

        doc.buckets = doc.bucketize(doc.minhashes)
        tBucketize = time.time() - t0 - tParse - tMinhash
        stats['bucketize'] = tBucketize

        # if 0 == int(1000*time.time()) % 20:
        #     # print 5% of the documents on average
        #     logging.info('%s %s %s', doc.ds_key, doc.doc_id, doc.buckets)
        data = {
                'ds_key': '%s' % doc.ds_key,
                'doc_id': '%s' % doc.doc_id,
                # Don't need to store minhashes
                # 'minhashes': doc.minhashes,
                'buckets': doc.buckets,
                }
        MatrixRow.insert_row(data = data)
        tDbWrite = time.time() - t0 - tParse - tMinhash - tBucketize
        stats['database'] = tDbWrite
        return doc

class MatrixRow(object):
    __metaclass__ = Table
    attrs = [
             'ds_key text',
             'doc_id text',
             'buckets list<int>',
             # Don't need to store minhashes
             # 'minhashes list<int>',
             ]
    p_keys = ['doc_id', 'ds_key']
    indexes = [('doc_buckets', 'buckets',)]

    @classmethod
    def _initialize(cls):
        # Make sure the underlying tables or data structures exist
        matrix_row = MatrixRow(name = cls.__name__, attrs = cls.attrs, p_keys = cls.p_keys, indexes = cls.indexes)
        return matrix_row

    @classmethod
    def create(cls):
        return cls._initialize()

    def calc_minhashes(self):
        def minhashes_for_shingles(shingles):
            def calc_onehash(shingle, seed):
                def c4_hash(shingle):
                    try:
                        h = struct.unpack('<i',shingle)[0]
                    except struct.error:
                        # We land here when the shingle has non-ascii characters in it.
                        size = 4
                        encoded = shingle.encode('utf-8')
                        int_hashes = [int(encoded[i:i + size].encode('hex'), 16) for i in range(len(encoded)/size)] 
                        h = reduce(operator.xor, int_hashes)
                    hash_val = h & settings.max_mask
                    return hash_val

                if self.sh_type == 'c4':
                    return operator.xor(c4_hash(shingle), long(seed)) % self.modulo
                else:
                    return operator.xor(compute_positive_hash(shingle), long(seed)) % self.modulo

            minhashes = [settings.max_mask for _ in xrange(self.hashes)]
            for shingle in shingles:
                for hno in xrange(self.hashes):
                    h_value = calc_onehash(shingle, self.seeds[hno])
                    minhashes[hno] = min(h_value, minhashes[hno])
            return minhashes
        ##########################################
        shingles = self.shingles()
        minhashes = minhashes_for_shingles(shingles)
        return minhashes
    
    def shingles(self):
        return MatrixRow.shingle_text(self.text, self.sh_type)

    @staticmethod
    def shingle_text(text, sh_type):
        retval = set(text.split()) if sh_type=='w' else set(_get_list_of_shingles(text))
        return retval
    
    def bucketize(self, minhashes):
        buckets = []
        try:
            band_bits = self.dataset.band_bits
        except AttributeError:
            matrix = self.dataset
            band_bits = int(math.ceil(math.log(matrix.bands, 2)))
            band_mask = (2**band_bits - 1)
            setattr(matrix, 'band_bits', band_bits)
            setattr(matrix, 'band_mask', band_mask)
            setattr(matrix, 'hash_mask', 2**(settings.max_bits - band_bits)-1)

        band_mask = self.dataset.band_mask
        hash_mask = self.dataset.hash_mask
        for band in xrange(self.dataset.bands):
            band_hash = (band_mask & band) * (hash_mask + 1)
            minhashes_in_band = [minhashes[band*self.rows + row] for row in xrange(self.rows)]
            minhashes_into_a_string = '-'.join([str(mh) for mh in minhashes_in_band])
            bucket = band_hash | (hash_mask & int(hashlib.md5(minhashes_into_a_string).hexdigest(), 16))
            buckets.append(DbInt.to_db(bucket))
        return buckets
