import sys
sys.path.insert(0, "/Users/edcollins/Documents/CS/4thYearProject/Code")
import numpy as np
from itertools import islice
from Dev.DataTools.LSTM_preproc.map import numpify
from numpy.random import choice
#from jtr.util.rs import DefaultRandomState

rs = np.random.RandomState(1337)
#rs = DefaultRandomState(1337)#new seed ignored if set previously


def get_buckets(data, order, structure):
    """
    Generates mapping between data instances and bucket-ID's.

    `data`: dict of nested sequences in which each top-level sequence has the same length,
        and all inner sequences have the __len__ attribute.
    `order`: (None or) tuple with data keys used for bucketing
        For example:
        ```list(data.keys()) = ["sentences1", "lengths1", "sentences2", "lengths2", "targets"]```
        and we want bucketing according to the lengths of inner sequences in "sentences1" and "sentences2":
        `order = ("sentences1", "sentences2")` performs bucketing on "sentences1", and within each bucket,
        again creates buckets according to "sentences2"
        (automatic bucketing will result in different "sentences2" bucket boundaries
        within each bucket according to "sentences1").
        `order = ("sentences2", "sentences1")`: vice versa, with "sentences2" for highest-level buckets
    `structure`: (None or) sequence with same length as `order`, each element is an integer or a list of integers
        For each position:
            - integer: denotes number of buckets, to be determined automatically
            - list: determines bucket boundaries. E.g.: [10, 20, 30] will result in 4 buckets
              (1) lengths 0-10, (2) lengths 11-20, (3) lengths 21-30, (4) lengths > 30
        For example:
        `order` = ("sentences1", "sentences2") and `structure` = (3, [10]) generates 6 buckets:
        within each of 3 partitions based on "sentences1",
        there is a bucket with instances of "sentences2" with length 10 or less,
        and one for lengths > 10.

    Returns:
        buckets2ids, ids2buckets
        dicts that map instance-id (index along 1st dimension of values in data) to bucket-id,
        and vice versa.
    """
    assert isinstance(data, dict)

    n_tot = len(list(data.values())[0])
    if order is None or structure is None:
        # all in 1 bucket, with id '(0)'
        buckets2ids = {'(0)': list(range(n_tot))}
        ids2buckets = dict(zip(list(range(n_tot)), ['(0)'] * n_tot))
        return buckets2ids, ids2buckets

    def _chunk(it, size):
        """returns iterator of chunks (tuples) from it (input iterator), with given size (last one may be shorter)"""
        it = iter(it)
        return iter(lambda: tuple(islice(it, size)), ())

    def _partition(_buckets2ids, _order, _structure):
        """update _buckets2ids according to _order and _structure"""
        # update all current buckets according to first item in _order and _structure
        buckets2ids_new = {}
        for bid, ids in sorted(_buckets2ids.items(), key=lambda x: x[0]):
            lengths = [len(data[_order[0]][id]) for id in ids]
            sorted_ids_lengths = sorted(zip(ids, lengths), key=lambda x: x[1])
            if isinstance(_structure[0], int):  # automatic bucketing
                size = len(lengths) // _structure[0] if len(lengths) % _structure[0] == 0 \
                    else 1 + (len(lengths) // _structure[0])
                buckets = list(_chunk([tup[0] for tup in sorted_ids_lengths], size))
            else:  # structure_is sequence of ints
                struct = list(sorted(_structure[0])) + [np.inf]
                bin_max, struct = struct[0], struct[1:]
                buckets = [[]]
                for id, l in sorted_ids_lengths:
                    if l > bin_max:  # never happens when bin_max = np.inf
                        bin_max, struct = struct[0], struct[1:]
                        buckets.append([])
                    buckets[-1].append(id)
            buckets2ids_new.update({tuple(list(bid) + [i]): list(bucket) for i, bucket in enumerate(buckets)})
        # call again if _order and _structure have more than 1 item
        if len(_order) > 1:
            buckets2ids_new = _partition(buckets2ids_new, _order[1:], _structure[1:])

        buckets2ids_new = {bid: bucket for bid, bucket in buckets2ids_new.items() if len(bucket) > 0}
        return buckets2ids_new


    buckets2ids = _partition({(): list(range(n_tot))}, order, structure)
    buckets2ids = {str(bid): buckets2ids[bid] for bid in buckets2ids}  # make bucket-ids strings (for random.choice)

    ids2buckets = {}
    for bid, bucket in buckets2ids.items():
        ids2buckets.update({id: bid for id in bucket})
    return buckets2ids, ids2buckets


def get_batches(data, batch_size=32, pad=0, bucket_order=None, bucket_structure=None, exact_epoch=False):
    """
    Creates generator that batches `data`.
    To avoid biases, it is advised to keep `bucket_order=None` and `bucket_structure=None` if computationally possible.
    (which will sample batches from all instances)

    Args:
        `data`: dict with (multi-dimensional) numpy arrays or (nested) lists;
            first inner dimension (`num_instances`) should be the same over all data values.
        `batch_size`: the desired batch size
        `pad`: padding symbol in case data contains lists of lists of different sizes
        `bucket_order`: argument `order` in get_buckets (list with keys); `None` if no bucketing
        `bucket_structure`: argument `structure` in get_buckets; `None` if no bucketing
        `exact_epoch`: if set to `True`, final batch per bucket may be smaller, but each instance will be seen exactly
            once during training. Default: `False`, to be certain during training
            that each instance per batch gets same weight in the total loss
            (but not all instances are observed per epoch if bucket sizes are no multiple of `batch_size`).

    Returns:
        a generator that generates a dict with same keys as `data`, and
        as values data batches consisting of `[batch_size x num_instances]` 2D numpy tensors
        (1st dimension is at most `batch_size` but may be smaller to cover all instances exactly once per epoch,
        if `exact_epoch=True`)
     """
    assert isinstance(data, dict)

    data0 = list(data.values())[0]
    if not isinstance(data0, np.ndarray):
        data_np = numpify(data, pad)  # still need original data for length-based bucketing
    else:
        data_np = data

    def get_bucket_probs(_buckets2instances):
        N = float(np.sum([len(ids) for ids in _buckets2instances.values()]))
        return {bid: len(ids) / N if N > 0. else 0. for bid, ids in _buckets2instances.items()}

    def shuffle_buckets(_buckets2instances):
        for bid in sorted(_buckets2instances.keys()):  # sorted: to keep deterministic
            rs.shuffle(_buckets2instances[bid])

    buckets2instances, _ = get_buckets(data, bucket_order, bucket_structure)
    n_buckets = len(buckets2instances)

    exact_epoch = True if len(data0) < n_buckets*batch_size else exact_epoch
    #if average instances/bucket smaller than batch_size: set exact_epoch = True
    #to avoid empty batches during debugging on small data samples

    def bucket_generator():
        buckets2instances, _ = get_buckets(data, bucket_order, bucket_structure)
        shuffle_buckets(buckets2instances)
        all_seen = False
        while not all_seen:
            bids, probs = zip(*sorted(get_bucket_probs(buckets2instances).items(), key=lambda x: x[0]))
            # sorted keys: to keep deterministic
            if np.sum(probs) == 0.:
                all_seen = True
            else:
                bid = rs.choice(bids, replace=False, p=probs)  # sample bucket according to remaining size
                batch_indices = buckets2instances[bid][:batch_size]
                buckets2instances[bid] = buckets2instances[bid][batch_size:]
                # if required by exact_epoch: also include last batch in bucket if too small
                if len(batch_indices) == batch_size or exact_epoch:
                    yield {k: data_np[k][batch_indices] for k in data_np}

    return GeneratorWithRestart(bucket_generator)


def get_feed_dicts(data_train_np, placeholders, batch_size, inst_length):
    # around 8 times faster as get_feed_dicts_old() with generator as it doesn't need to go over the whole training data every time during training
    data_train_batched = []
    realsamp = int(inst_length/batch_size)
    additionsamp = inst_length%batch_size
    if additionsamp != 0:
        realsamp += 1
    ids1 = choice(range(0, inst_length), inst_length-additionsamp, replace=False)  # sample without replacement so we get every sample once
    ids2 = choice(range(0, inst_length), additionsamp, replace=True)  # sample a few additional ones to fill up batch
    ids = np.append(ids1, ids2)

    start = 0
    for i in range(0, realsamp):
        batch_i = {}
        if i != 0:
            start = i * batch_size
        if i != realsamp:
            ids_sup = ids[start:((i+1)*batch_size)]
        else:
            ids_sup = ids[start:realsamp]
        for key, value in data_train_np.items():
            batch_i[placeholders[key]] = [data_train_np[key][ii] for ii in ids_sup]

        data_train_batched.append(batch_i)

    return data_train_batched


def get_feed_dicts_old(data, placeholders, batch_size=32, pad=0, bucket_order=None, bucket_structure=None, exact_epoch=False):
    """Creates feed dicts for all batches with a given batch size.

    Args:
        `data` (dict): The input data for the feed dicts.
        `placeholders` (dict): The TensorFlow placeholders for the data
            (placeholders.keys() must form a subset of data.keys()).
        `batch_size` (int): The batch size for the data.
        `pad` (int): Padding symbol index to pad lists of different sizes.
        `bucket_order`: argument `order` in get_buckets (list with keys); `None` if no bucketing
        `bucket_structure`: argument `structure` in get_buckets; `None` if no bucketing
        `exact_epoch`: if set to `True`, final batch per bucket may be smaller, but each instance will be seen exactly
            once during training. Default: `False`, to be certain during training
            that each instance per batch gets same weight in the total loss.

    Returns:
        GeneratorWithRestart: Generator that yields a feed_dict for each
        iteration. A feed dict consists of '{ placeholder : data-batch }` key-value pairs.
    """
    assert isinstance(data, dict) and isinstance(placeholders, dict)
    assert set(placeholders.keys()).issubset(set(data.keys())), \
        'data keys %s \nnot compatible with placeholder keys %s' % (set(placeholders.keys()), set(data.keys()))

    def generator():
        batches = get_batches(data, batch_size, pad, bucket_order, bucket_structure, exact_epoch)
        # fixme: this is potentially inefficient as it might be called every time we retrieve a batch
        # todo: measure and fix if significant impact
        mapped = map(lambda xs: {placeholders[k]: xs[k] for k in placeholders}, batches)
        #for each key in placeholders dict, pair the placeholder with the corresponding batch dict value
        for x in mapped:
            yield x

    return GeneratorWithRestart(generator)


class GeneratorWithRestart(object):
    def __init__(self, iterator):
        self.iterator = iterator

    def __iter__(self):
        return self.iterator()
