def bubble_sort(arr):
    """Sort a list using the bubble sort algorithm (in-place)."""
    n = len(arr)
    for i in range(n):
        swapped = False
        for j in range(0, n - i - 1):
            if arr[j] > arr[j + 1]:
                arr[j], arr[j + 1] = arr[j + 1], arr[j]
                swapped = True
        if not swapped:
            break
    return arr


if __name__ == "__main__":
    # Example usage
    example = [64, 34, 25, 12, 22, 11, 90]
    print("Original:", example)
    bubble_sort(example)
    print("Sorted:  ", example)